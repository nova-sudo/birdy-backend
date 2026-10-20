"""
tests/test_daily_metrics.py
---------------------------
get_daily_metrics: the chat's one day-by-day series, built from the same daily
Meta fetch get_metrics_by_day_windows already makes.
"""

from datetime import date, timedelta

import pytest

from ai.tools import multi_window_tools as mw


def test_daily_series_zero_fills_and_derives_cpl_and_ctr():
    rows = [
        {"date_start": "2026-10-01", "spend": 50.0, "impressions": 1000, "clicks": 20, "results": 5},
        {"date_start": "2026-10-03", "spend": 30.0, "impressions": 0, "clicks": 0, "results": 0},
    ]
    out = mw._daily_series(rows, "2026-10-01", "2026-10-03")
    assert [d["date"] for d in out] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert out[0] == {"date": "2026-10-01", "spend": 50.0, "leads": 5, "clicks": 20,
                      "impressions": 1000, "cpl": 10.0, "ctr": 2.0}
    assert out[1]["spend"] == 0 and out[1]["cpl"] is None
    # spend with no leads: CPL stays unknown rather than 0 or infinity
    assert out[2]["cpl"] is None and out[2]["ctr"] is None


@pytest.fixture
def fake_meta(monkeypatch):
    calls = []

    async def fake_token(user_id, client):
        return {"access_token": "tok"}

    async def fake_fetch(ad_account_id, token, since, until, campaign_ids=None):
        calls.append((ad_account_id, since, until))
        return {"days": [{"date_start": since, "spend": 10.0, "impressions": 100, "clicks": 4, "results": 2}]}

    monkeypatch.setattr(mw, "get_facebook_token", fake_token)
    monkeypatch.setattr(mw, "_fetch_daily_insights", fake_fetch)
    return calls


async def test_daily_metrics_per_client_and_combined(mock_db, fake_meta):
    await mock_db["client_groups"].insert_many([
        {"user_id": "u1", "id": "g1", "name": "Aura", "meta_ad_account_id": "act_1", "ad_account_currency": "GBP"},
        {"user_id": "u1", "id": "g2", "name": "BBL", "meta_ad_account_id": "act_2", "ad_account_currency": "GBP"},
        {"user_id": "u1", "id": "g3", "name": "No Meta"},
    ])
    out = await mw.get_daily_metrics(mock_db, "u1", days=7)
    today = date.today()
    assert out["since"] == (today - timedelta(days=7)).isoformat()
    assert out["until"] == (today - timedelta(days=1)).isoformat()
    assert len(fake_meta) == 2
    by_name = {g["group_name"]: g for g in out["groups"]}
    assert len(by_name["Aura"]["days"]) == 7 and by_name["Aura"]["days"][0]["cpl"] == 5.0
    assert "meta_error" in by_name["No Meta"]
    assert out["combined"]["currency"] == "GBP"
    assert out["combined"]["days"][0]["spend"] == 20.0 and out["combined"]["days"][0]["leads"] == 4


async def test_daily_metrics_skips_combined_across_currencies(mock_db, fake_meta):
    await mock_db["client_groups"].insert_many([
        {"user_id": "u1", "id": "g1", "name": "Aura", "meta_ad_account_id": "act_1", "ad_account_currency": "GBP"},
        {"user_id": "u1", "id": "g2", "name": "Acme", "meta_ad_account_id": "act_2", "ad_account_currency": "USD"},
    ])
    out = await mw.get_daily_metrics(mock_db, "u1", days=3)
    assert "combined" not in out


@pytest.mark.parametrize("days", [1, 91, "lots"])
async def test_daily_metrics_rejects_bad_ranges(mock_db, days):
    assert "error" in await mw.get_daily_metrics(mock_db, "u1", days=days)
