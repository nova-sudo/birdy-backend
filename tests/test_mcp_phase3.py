"""Data behind the phase 3 cards: the CPL driver split (compare_periods) and get_budget_pacing."""

from datetime import date

import pytest

from ai.mcp import pacing_mcp
from ai.mcp.compare_mcp import _cpl_drivers


def _row(spend, imp, clk, leads):
    return {"spend": spend, "impressions": imp, "clicks": clk, "total_leads": leads}


def test_cpl_drivers_add_up_to_the_cpl_change():
    d = _cpl_drivers(_row(196, 20000, 400, 24), _row(131.5, 12000, 130, 7))
    assert d["from"] == 8.17 and d["to"] == 18.79
    assert round(sum(st["effect"] for st in d["steps"]), 2) == round(d["to"] - d["from"], 2)
    assert [st["key"] for st in d["steps"]] == ["cpm", "ctr", "lead_rate"]
    assert d["biggest"] == "ctr"  # CTR fell from 2% to 1.08%: it pushed CPL up the most


def test_better_ctr_lowers_cpl():
    d = _cpl_drivers(_row(100, 10000, 100, 10), _row(100, 10000, 200, 20))
    ctr = next(st for st in d["steps"] if st["key"] == "ctr")
    assert ctr["effect"] < 0 and d["biggest"] == "ctr"


@pytest.mark.parametrize("a,b", [(_row(100, 1000, 10, 0), _row(100, 1000, 10, 5)), (_row(0, 0, 0, 0), _row(1, 1, 1, 1))])
def test_cpl_drivers_need_leads_in_both_periods(a, b):
    assert _cpl_drivers(a, b) is None


def test_cpl_drivers_flat_has_no_biggest():
    r = _row(100, 10000, 100, 10)
    assert _cpl_drivers(r, dict(r))["biggest"] is None


def test_pace_row_states():
    today = date(2026, 11, 18)  # 17.5 of 30 days: 58.3% expected
    over = pacing_mcp.pace_row(1840, 2000, today)
    assert over["status"] == "over" and over["runs_out_on"] == "2026-11-20"
    assert pacing_mcp.pace_row(1150, 2000, today)["status"] == "on_pace"
    under = pacing_mcp.pace_row(610, 2000, today)
    assert under["status"] == "under" and under["runs_out_on"] is None
    assert pacing_mcp.pace_row(2100, 2000, today)["runs_out_on"] == "already"
    assert pacing_mcp.pace_row(1180, 2000, today)["runs_out_on"] is None  # last day of the month is on pace
    none = pacing_mcp.pace_row(500, None, today)
    assert none["status"] == "no_budget" and none["used_pct"] is None and none["projected"] == round(500 / 17.5 * 30, 2)


@pytest.mark.asyncio
async def test_get_budget_pacing_reads_this_month_and_the_monthly_spend_target(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": u, "name": "Glow", "targets": {"monthly_spend": 1000},
         "facebook_cache": {"currency": "GBP", "presets": {"this_month": {"metrics": {"insights": {"spend": "900"}}}}}},
        {"id": "g2", "user_id": u, "name": "Smile", "facebook_cache": {"this_month": {"metrics": {"insights": {"spend": 50}}}}},
        {"id": "g3", "user_id": u, "name": "Gone", "client_status": "Inactive", "targets": {"monthly_spend": 5}},
        {"id": "g4", "user_id": "bob@example.com", "name": "Bob's", "targets": {"monthly_spend": 5}},
    ])
    out = await pacing_mcp.get_budget_pacing.fn()
    assert [c["client"]["name"] for c in out["clients"]] == ["Glow", "Smile"]
    glow, smile = out["clients"]
    assert glow["budget"] == 1000 and glow["spend"] == 900 and glow["currency"] == "GBP" and glow["used_pct"] == 90
    assert smile["status"] == "no_budget" and smile["spend"] == 50
    assert out["summary"]["no_budget"] == 1
