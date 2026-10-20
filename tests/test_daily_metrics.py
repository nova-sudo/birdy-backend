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


def _day(iso, spend, leads):
    return {"date": iso, "spend": spend, "leads": leads, "clicks": 10, "impressions": 1000, "cpl": None, "ctr": None}


def test_summary_compares_with_the_equal_period_before():
    previous = [_day("2026-10-01", 100, 10), _day("2026-10-02", 100, 10)]
    days = [_day("2026-10-03", 150, 10), _day("2026-10-04", 150, 5)]
    out = mw._summarize(days, previous)
    assert out["total"]["spend"] == 300 and out["total"]["leads"] == 15 and out["total"]["cpl"] == 20.0
    assert out["previous_period"]["spend"] == 200 and out["previous_period"]["cpl"] == 10.0
    assert out["previous_period"]["to"] == "2026-10-02"
    assert out["change_pct"] == {"spend": 50.0, "leads": -25.0, "cpl": 100.0, "ctr": 0.0}


def test_summary_with_no_prior_spend_has_no_change():
    out = mw._summarize([_day("2026-10-03", 150, 10)], [_day("2026-10-02", 0, 0)])
    assert out["change_pct"]["spend"] is None and out["change_pct"]["cpl"] is None


def test_split_series_returns_the_range_and_the_one_before():
    rows = [{"date_start": "2026-10-01", "spend": 5.0, "impressions": 10, "clicks": 1, "results": 1}]
    days, previous = mw._split_series(rows, "2026-10-01", "2026-10-04", "2026-10-06")
    assert [d["date"] for d in days] == ["2026-10-04", "2026-10-05", "2026-10-06"]
    assert [d["date"] for d in previous] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert previous[0]["spend"] == 5.0


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
    # one fetch per client covers the range and the 7 days before it
    assert {(since, until) for _, since, until in fake_meta} == {((today - timedelta(days=14)).isoformat(), out["until"])}
    by_name = {g["group_name"]: g for g in out["groups"]}
    # the fake's one row falls on the first day fetched, in the previous period
    assert len(by_name["Aura"]["days"]) == 7 and by_name["Aura"]["days"][0]["date"] == out["since"]
    assert by_name["Aura"]["summary"]["total"]["spend"] == 0
    assert by_name["Aura"]["summary"]["previous_period"]["spend"] == 10.0
    assert by_name["Aura"]["summary"]["previous_period"]["cpl"] == 5.0
    assert "meta_error" in by_name["No Meta"]
    assert out["combined"]["currency"] == "GBP"
    assert out["combined"]["summary"]["previous_period"]["spend"] == 20.0
    assert out["combined"]["summary"]["previous_period"]["leads"] == 4
    assert out["combined"]["summary"]["total"]["to"] == out["until"]


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


async def test_client_page_pins_daily_metrics_to_its_client(mock_db):
    """On a client page an omitted group_ids would mean every client."""
    import json
    from ai.orchestrator import run_chat
    from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
    from ai.tools.registry import ToolRegistry

    seen = {}

    async def fake_daily(db, user_id, days=30, group_ids=None, **kwargs):
        seen["group_ids"] = group_ids
        return {"groups": []}

    reg = ToolRegistry()
    reg.register(name="get_daily_metrics", description="stub", parameters={"type": "object", "properties": {}},
                 executor=fake_daily)

    class Provider(BaseLLMProvider):
        model = "stub"
        def __init__(self):
            self.r = [ProviderResponse(content=None, finish_reason="tool_calls", tool_calls=[
                ToolCall(id="t1", name="get_daily_metrics", arguments=json.dumps({"days": 30}))]),
                ProviderResponse(content="ok")]
        async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
            return self.r.pop(0)

    await run_chat(provider=Provider(), tool_registry=reg, db=mock_db, user_id="u1", message="chart",
                   page="client_detail", client_group_id="grp-A", client_name="Acme")
    assert seen["group_ids"] == ["grp-A"]
