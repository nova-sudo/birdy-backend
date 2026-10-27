"""
tests/test_meta_ad_daily.py
---------------------------
Daily ad-level Meta insights for the agent's ad history: request shape, paging,
the None-vs-[] contract, permission detection, and that stored rows read back
through the agent's own loader (ai.agents.snapshot.load_ad_days).
"""

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ai.agents.snapshot import load_ad_days
from integrations.facebook_utils.meta_ad_daily import (
    MetaPermissionError,
    fetch_ad_daily,
    store_ad_daily,
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
            "integrations.facebook_utils.meta_ad_daily.httpx.AsyncClient", factory)
    return apply


@pytest.mark.asyncio
async def test_fetch_builds_params_and_follows_paging(patch_client):
    seen = []
    nxt = "https://graph.facebook.com/v25.0/act_1/insights?after=CURSOR&access_token=T"
    patch_client([
        {"body": {"data": [{"ad_id": "a1", "date_start": "2026-10-01", "spend": "5"}],
                  "paging": {"next": nxt}}},
        {"body": {"data": [{"ad_id": "a1", "date_start": "2026-10-02", "spend": "6"}]}},
    ], seen)

    rows = await fetch_ad_daily("act_1", "T", D1, D2, campaign_ids=["c1"])

    assert [r["date_start"] for r in rows] == ["2026-10-01", "2026-10-02"]
    assert len(seen) == 2
    q = parse_qs(urlparse(seen[0]).query)
    assert q["level"] == ["ad"]
    assert q["time_increment"] == ["1"]
    assert json.loads(q["time_range"][0]) == {"since": "2026-10-01", "until": "2026-10-30"}
    assert q["fields"] == ["ad_id,ad_name,adset_id,campaign_id,campaign_name,spend,actions,date_start"]
    assert q["limit"] == ["500"]
    assert json.loads(q["filtering"][0]) == [
        {"field": "spend", "operator": "GREATER_THAN", "value": 0},
        {"field": "campaign.id", "operator": "IN", "value": ["c1"]},
    ]
    # Second page is Meta's own `next` URL, untouched.
    assert seen[1] == nxt


@pytest.mark.asyncio
async def test_unscoped_filter_is_spend_only(patch_client):
    seen = []
    patch_client([{"body": {"data": []}}], seen)

    assert await fetch_ad_daily("act_1", "T", D1, D2, campaign_ids=None) == []

    q = parse_qs(urlparse(seen[0]).query)
    assert json.loads(q["filtering"][0]) == [
        {"field": "spend", "operator": "GREATER_THAN", "value": 0}]


@pytest.mark.asyncio
async def test_fetch_returns_none_on_failure_and_empty_for_no_campaigns(patch_client):
    seen = []
    patch_client([{"status": 500, "body": {"error": {"code": 1, "message": "boom"}}}], seen)

    assert await fetch_ad_daily("act_1", "T", D1, D2, campaign_ids=[]) == []
    assert seen == []  # no request for an empty scope
    assert await fetch_ad_daily("act_1", "T", D1, D2, campaign_ids=None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [200, 10])
async def test_fetch_permission_error_raises(patch_client, code):
    patch_client([{"status": 403, "body": {"error": {
        "code": code, "message": "(#200) Ad account owner has NOT grant ads_read"}}}], [])

    with pytest.raises(MetaPermissionError, match="ads_read"):
        await fetch_ad_daily("act_1", "T", D1, D2, campaign_ids=None)


@pytest.mark.asyncio
async def test_store_rows_load_back_through_load_ad_days(mock_db):
    base = {"ad_id": "a1", "ad_name": "Ad", "adset_id": "s1",
            "campaign_id": "c1", "campaign_name": "C"}
    rows = [
        {**base, "date_start": "2026-10-01", "spend": "12.50"},  # no actions: zero-lead day
        {**base, "date_start": "2026-10-02", "spend": "8",
         "actions": [{"action_type": "lead", "value": "2"}]},
    ]
    kw = dict(user_id="u", client_group_id="g", ad_account_id="act_1")

    assert await store_ad_daily(mock_db, rows=rows, **kw) == 2
    days = await load_ad_days(mock_db, "u", "g", date(2026, 9, 30), date(2026, 10, 3))
    assert [(d.day.isoformat(), d.spend, d.leads) for d in days["a1"]] == [
        ("2026-10-01", 12.5, 0), ("2026-10-02", 8.0, 2)]

    doc = await mock_db["facebook_ad_insights"].find_one({"ad_id": "a1", "date_start": "2026-10-01"})
    assert doc["source"] == "agent_history"
    assert doc["date_stop"] == "2026-10-01"
    assert doc["ad_account_id"] == "act_1"
    assert doc["insight_data"] == {"spend": "12.50", "actions": []}

    # Re-storing the same days overwrites rather than duplicating.
    rows[0]["spend"] = "20"
    assert await store_ad_daily(mock_db, rows=rows, **kw) == 2
    assert await mock_db["facebook_ad_insights"].count_documents({}) == 2
    days = await load_ad_days(mock_db, "u", "g", date(2026, 9, 30), date(2026, 10, 3))
    assert days["a1"][0].spend == 20.0


@pytest.mark.asyncio
async def test_store_empty_rows_writes_nothing(mock_db):
    assert await store_ad_daily(mock_db, user_id="u", client_group_id="g",
                                ad_account_id="act_1", rows=[]) == 0
