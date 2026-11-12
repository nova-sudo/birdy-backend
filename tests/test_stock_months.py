"""The whole-month report rows (BirdyHub redesign, spec section 2): sums of the month's ad days per client,
decided cards counted in the agent's own timezone, and a route that refuses bad and future months."""

from datetime import datetime

from ai.agents.stock.months import month_rows
from ai.agents.store import AD_DAYS, AGENTS, CARDS
from test_stock_router import OTHER, api  # noqa: F401 - api is a fixture
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture


async def _day(db, gid, day, spend, leads, ad="ad_x"):
    await db[AD_DAYS].insert_one({
        "user_id": USER, "client_group_id": gid, "ad_id": ad, "campaign_id": "c1", "ad_name": "X",
        "date_start": day, "insight_data": {"spend": str(spend), "actions": [{"action_type": "lead", "value": str(leads)}]}})


async def test_month_sums_ad_days(mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    await _day(mock_db, "g1", "2026-10-01", 10.5, 1)
    await _day(mock_db, "g1", "2026-10-31", 4.5, 2, ad="ad_y")
    await _day(mock_db, "g1", "2026-09-30", 100, 1)   # outside the month
    await _day(mock_db, "g1", "2026-11-01", 100, 1)   # outside the month
    out = await month_rows(mock_db, agent, "2026-10")
    assert out["month"] == "2026-10" and out["range"] == "1 Oct – 31 Oct"
    [row] = out["rows"]
    assert row["client_group_id"] == "g1" and row["client_name"] == "Client g1"
    assert (row["spend"], row["leads"], row["cpl"]) == (15.0, 3, 5.0)
    assert row["zone"] == "green" and row["decided"] == 0


async def test_month_no_rows_is_null_figures(mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    [row] = (await month_rows(mock_db, agent, "2026-10"))["rows"]
    assert (row["spend"], row["leads"], row["cpl"]) == (None, None, None)
    assert row["zone"] == "no_data"


async def test_month_counts_decided(mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    base = {"agent_id": agent["_id"], "client_group_id": "g1", "status": "applied"}
    await mock_db[CARDS].insert_many([
        {"_id": "a", **base, "decided_at": datetime(2026, 10, 3, 9)},
        {"_id": "b", **base, "decided_at": datetime(2026, 10, 20, 9)},
        {"_id": "c", **base, "decided_at": datetime(2026, 11, 2, 9)},           # next month
        {"_id": "d", **base, "decided_at": None, "status": "open"},             # not decided
        {"_id": "e", **base, "decided_at": datetime(2026, 10, 4), "preview": True},
        {"_id": "f", **base, "agent_id": "other", "decided_at": datetime(2026, 10, 4)}])
    [row] = (await month_rows(mock_db, agent, "2026-10"))["rows"]
    assert row["decided"] == 2


async def test_month_uses_agent_timezone(mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"scope.schedule.tz": "Europe/London"}})
    agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    # 23:30 UTC on 31 Aug is 00:30 on 1 Sep in London (BST): September's, not August's.
    await mock_db[CARDS].insert_one({"_id": "late", "agent_id": agent["_id"], "client_group_id": "g1",
                                     "status": "applied", "decided_at": datetime(2026, 8, 31, 23, 30)})
    [aug] = (await month_rows(mock_db, agent, "2026-08"))["rows"]
    [sep] = (await month_rows(mock_db, agent, "2026-09"))["rows"]
    assert (aug["decided"], sep["decided"]) == (0, 1)


async def test_month_route_rejects_future_and_bad(api, mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    aid = agent["_id"]
    for bad in ("2026-1", "202610", "2026-13", "abcd-ef"):
        r = api.get(f"/api/agents/{aid}/months/{bad}")
        assert r.status_code == 400 and r.json()["detail"] == "bad_month", bad
    r = api.get(f"/api/agents/{aid}/months/2026-12")   # NOW is 3 Nov 2026
    assert r.status_code == 400 and r.json()["detail"] == "future_month"
    r = api.get(f"/api/agents/{aid}/months/2026-11")
    assert r.status_code == 200 and r.json()["month"] == "2026-11" and r.json()["range"] == "1 Nov – 30 Nov"


async def test_month_route_other_user_404(api, mock_db):
    agent = await _setup(mock_db, "g1", days=False)
    api.state["user"] = OTHER
    assert api.get(f"/api/agents/{agent['_id']}/months/2026-10").status_code == 404
