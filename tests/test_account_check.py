"""build_account_check / check_my_accounts: per-client attention verdicts from the same daily-metrics code
as get_daily_metrics, plus alerts fired and open agent cards. No LLM."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import watch_mcp
from ai.tools import multi_window_tools as mw
from services import account_check
from services.account_check import build_account_check

pytestmark = pytest.mark.asyncio

TODAY = date.today()
CUR_DAY = (TODAY - timedelta(days=1)).isoformat()       # last day of the period
PRIOR_DAY = (TODAY - timedelta(days=14)).isoformat()    # first day fetched = inside the prior period (days=7)


@pytest.fixture
def meta(monkeypatch):
    """acct -> {"cur": (spend, leads), "prior": (spend, leads)} | Exception | {"error": str}."""
    data: dict = {}
    state = {"running": 0, "peak": 0}

    async def fake_token(user_id, client):
        return {"access_token": "tok"}

    async def fake_fetch(ad_account_id, token, since, until, campaign_ids=None):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await asyncio.sleep(0)
            spec = data[ad_account_id]
            if isinstance(spec, Exception):
                raise spec
            if "error" in spec:
                return spec
            rows = []
            for key, day in (("prior", since), ("cur", until)):
                spend, leads = spec.get(key, (0, 0))
                if spend or leads:
                    rows.append({"date_start": day, "spend": float(spend), "impressions": 100, "clicks": 5,
                                 "results": leads})
            return {"days": rows}
        finally:
            state["running"] -= 1

    monkeypatch.setattr(mw, "get_facebook_token", fake_token)
    monkeypatch.setattr(mw, "_fetch_daily_insights", fake_fetch)
    data["_state"] = state
    return data


async def _client(db, cid="g1", name="Acme", user="u1", acct=None, **extra):
    doc = {"user_id": user, "id": cid, "name": name, "meta_ad_account_id": acct or f"act_{cid}",
           "ad_account_currency": "USD", **extra}
    await db["client_groups"].insert_one(doc)


async def _check(db, user="u1", days=7):
    return await build_account_check(db, db.client, user, days)


def _one(out, cid="g1"):
    return next(c for c in out["clients"] if c["client"]["id"] == cid)


async def test_shape_and_numbers(mock_db, meta):
    await _client(mock_db, targets={"cpl": 40})
    meta["act_g1"] = {"cur": (80, 4), "prior": (60, 3)}
    out = await _check(mock_db)
    assert out["period"] == {"start": (TODAY - timedelta(days=7)).isoformat(), "end": CUR_DAY,
                             "prior_start": (TODAY - timedelta(days=14)).isoformat(),
                             "prior_end": (TODAY - timedelta(days=8)).isoformat()}
    c = _one(out)
    assert c["client"] == {"id": "g1", "name": "Acme"}
    assert (c["spend"], c["leads"], c["cpl"], c["target_cpl"]) == (80.0, 4, 20.0, 40)
    assert c["prior"] == {"spend": 60.0, "leads": 3, "cpl": 20.0}
    assert c["change_pct"] == {"spend": 33.3, "leads": 33.3, "cpl": 0.0}
    assert (c["alerts_fired"], c["open_agent_cards"], c["attention"], c["reasons"]) == (0, 0, "ok", [])
    assert out["summary"] == {"high": 0, "medium": 0, "ok": 1}


async def test_cpl_exactly_1_2x_target_is_not_high(mock_db, meta):
    await _client(mock_db, targets={"cpl": 50})
    meta["act_g1"] = {"cur": (120, 2), "prior": (120, 2)}  # CPL 60 = 1.2 x 50
    assert _one(await _check(mock_db))["attention"] == "ok"


async def test_cpl_over_1_2x_target_is_high(mock_db, meta):
    await _client(mock_db, targets={"cpl": 50})
    meta["act_g1"] = {"cur": (121, 2), "prior": (121, 2)}  # CPL 60.5 = 1.21 x 50
    c = _one(await _check(mock_db))
    assert c["attention"] == "high"
    assert c["reasons"] == ["CPL $60.50 is 21% over the $50 target"]


async def test_client_with_no_target_never_high_on_cpl(mock_db, meta):
    await _client(mock_db)
    meta["act_g1"] = {"cur": (5000, 2), "prior": (5000, 2)}
    c = _one(await _check(mock_db))
    assert c["target_cpl"] is None and c["attention"] == "ok"


async def test_spend_stopped_is_high(mock_db, meta):
    await _client(mock_db)
    meta["act_g1"] = {"cur": (0, 0), "prior": (90, 3)}
    c = _one(await _check(mock_db))
    assert c["attention"] == "high"
    assert c["reasons"] == ["Spend stopped: $90 in the prior period, nothing in this one"]


async def test_no_prior_spend_and_no_spend_is_ok(mock_db, meta):
    await _client(mock_db)
    meta["act_g1"] = {}
    assert _one(await _check(mock_db))["attention"] == "ok"


async def test_alerts_fired_in_period_make_high(mock_db, meta):
    await _client(mock_db)
    await _client(mock_db, cid="g2", name="Other")
    meta["act_g1"] = meta["act_g2"] = {"cur": (50, 5), "prior": (50, 5)}
    now = datetime.utcnow()
    await mock_db["alerts"].insert_many([
        {"id": "a1", "user_id": "u1", "target_group_ids": ["g1"]},
        {"id": "a2", "user_id": "u1", "target_group_ids": ["g1", "g2"]},
        {"id": "a3", "user_id": "u1", "target_group_ids": ["g2"]},
    ])
    await mock_db["alert_notifications"].insert_many([
        # a1 fired three times in 15-minute passes: it is one alert
        {"alert_id": "a1", "user_id": "u1", "triggered_at": now - timedelta(hours=1)},
        {"alert_id": "a1", "user_id": "u1", "triggered_at": now - timedelta(minutes=45)},
        {"alert_id": "a1", "user_id": "u1", "triggered_at": now - timedelta(minutes=30)},
        # a per-client firing carries its own group_id, which wins over the alert's targets
        {"alert_id": "a2", "user_id": "u1", "group_id": "g2", "triggered_at": now - timedelta(days=2)},
        # before the period
        {"alert_id": "a3", "user_id": "u1", "triggered_at": now - timedelta(days=30)},
    ])
    out = await _check(mock_db)
    g1, g2 = _one(out), _one(out, "g2")
    assert g1["alerts_fired"] == 1 and g1["attention"] == "high"
    assert g1["reasons"] == ["1 alert fired in this period"]
    assert g2["alerts_fired"] == 1 and g2["attention"] == "high"


async def test_cpl_rise_of_25_pct_is_medium_24_is_not(mock_db, meta):
    await _client(mock_db)
    await _client(mock_db, cid="g2", name="B")
    meta["act_g1"] = {"cur": (100, 2), "prior": (80, 2)}   # CPL 50 vs 40 = +25%
    meta["act_g2"] = {"cur": (99, 2), "prior": (80, 2)}    # CPL 49.5 vs 40 = +23.75%
    out = await _check(mock_db)
    assert _one(out)["attention"] == "medium"
    assert _one(out)["reasons"] == ["CPL up 25% vs the prior period ($40 to $50)"]
    assert _one(out, "g2")["attention"] == "ok"


async def test_leads_drop_of_30_pct_is_medium(mock_db, meta):
    await _client(mock_db)
    meta["act_g1"] = {"cur": (70, 7), "prior": (100, 10)}
    c = _one(await _check(mock_db))
    assert c["attention"] == "medium"
    assert c["reasons"] == ["Leads down 30% vs the prior period (10 to 7)"]


async def test_leads_drop_ignored_below_5_prior_leads(mock_db, meta):
    await _client(mock_db)
    meta["act_g1"] = {"cur": (10, 1), "prior": (40, 4)}  # -75% leads but CPL 10 vs 10
    assert _one(await _check(mock_db))["attention"] == "ok"


async def test_open_agent_cards_make_medium(mock_db, meta):
    await _client(mock_db)
    await _client(mock_db, cid="g2", name="B")
    meta["act_g1"] = meta["act_g2"] = {"cur": (50, 5), "prior": (50, 5)}
    await mock_db["agent_cards"].insert_many([
        {"_id": "c1", "user_id": "u1", "agent_id": "stock", "client_group_id": "g1", "status": "open"},
        {"_id": "c2", "user_id": "u1", "agent_id": "custom", "client_group_id": "g1", "status": "flagged"},
        {"_id": "c3", "user_id": "u1", "agent_id": "stock", "client_group_id": "g1", "status": "applied"},
        {"_id": "c4", "user_id": "u1", "agent_id": "stock", "client_group_id": "g1", "status": "dismissed"},
    ])
    out = await _check(mock_db)
    assert _one(out)["open_agent_cards"] == 2 and _one(out)["attention"] == "medium"
    assert _one(out)["reasons"] == ["2 open agent cards waiting for review"]
    assert _one(out, "g2")["open_agent_cards"] == 0


async def test_currency_symbol_follows_the_client(mock_db, meta):
    await _client(mock_db, targets={"cpl": 10}, ad_account_currency="GBP")
    meta["act_g1"] = {"cur": (200, 2), "prior": (200, 2)}
    assert _one(await _check(mock_db))["reasons"] == ["CPL £100 is 900% over the £10 target"]


async def test_sort_by_attention_then_spend(mock_db, meta):
    for cid, spend in (("ok_big", 900), ("ok_small", 10), ("med", 50), ("high_small", 20), ("high_big", 60)):
        await _client(mock_db, cid=cid, name=cid)
    meta["act_ok_big"] = {"cur": (900, 9), "prior": (900, 9)}
    meta["act_ok_small"] = {"cur": (10, 1), "prior": (10, 1)}
    meta["act_med"] = {"cur": (50, 5), "prior": (50, 5)}
    meta["act_high_small"] = {"cur": (20, 2), "prior": (40, 2)}
    meta["act_high_big"] = {"cur": (60, 3), "prior": (60, 3)}
    await mock_db["agent_cards"].insert_one(
        {"_id": "c", "user_id": "u1", "agent_id": "a", "client_group_id": "med", "status": "open"})
    await mock_db["client_groups"].update_many({"id": {"$in": ["high_small", "high_big"]}},
                                               {"$set": {"targets": {"cpl": 1}}})
    out = await _check(mock_db)
    assert [c["client"]["id"] for c in out["clients"]] == ["high_big", "high_small", "med", "ok_big", "ok_small"]
    assert out["summary"] == {"high": 2, "medium": 1, "ok": 2}


async def test_scoped_to_user(mock_db, meta):
    await _client(mock_db, user="alice", cid="ga", name="Alice Co")
    await _client(mock_db, user="bob", cid="gb", name="Bob Co")
    meta["act_ga"] = meta["act_gb"] = {"cur": (50, 5), "prior": (50, 5)}
    await mock_db["agent_cards"].insert_one(
        {"_id": "c", "user_id": "alice", "agent_id": "a", "client_group_id": "ga", "status": "open"})
    await mock_db["alerts"].insert_one({"id": "al", "user_id": "alice", "target_group_ids": ["ga"]})
    await mock_db["alert_notifications"].insert_one(
        {"alert_id": "al", "user_id": "alice", "group_id": "ga", "triggered_at": datetime.utcnow()})
    bob = await _check(mock_db, user="bob")
    assert [c["client"]["name"] for c in bob["clients"]] == ["Bob Co"]
    assert bob["clients"][0]["attention"] == "ok"
    alice = await _check(mock_db, user="alice")
    assert [c["client"]["name"] for c in alice["clients"]] == ["Alice Co"]
    assert alice["clients"][0]["alerts_fired"] == 1 and alice["clients"][0]["open_agent_cards"] == 1


async def test_client_without_meta_is_ok_not_an_error(mock_db, meta):
    await mock_db["client_groups"].insert_one({"user_id": "u1", "id": "g1", "name": "No Meta"})
    c = _one(await _check(mock_db))
    assert c["attention"] == "ok" and c["reasons"] == ["No Meta ad account connected"]
    assert c["spend"] is None and c["cpl"] is None


async def test_one_failing_client_does_not_break_the_rest(mock_db, meta):
    for cid in ("g1", "g2", "g3"):
        await _client(mock_db, cid=cid, name=cid)
    meta["act_g1"] = {"cur": (50, 5), "prior": (50, 5)}
    meta["act_g2"] = RuntimeError("boom")
    meta["act_g3"] = {"error": "Meta token expired or missing permissions. Please reconnect Meta."}
    out = await _check(mock_db)
    assert _one(out, "g1")["attention"] == "ok"
    assert _one(out, "g2")["attention"] == "medium"
    assert _one(out, "g2")["reasons"] == ["Couldn't load data: boom"]
    assert _one(out, "g3")["attention"] == "medium"
    assert _one(out, "g3")["reasons"][0].startswith("Couldn't load data: Meta token expired")


async def test_inactive_clients_are_skipped(mock_db, meta):
    await _client(mock_db, client_status="Inactive")
    assert (await _check(mock_db))["clients"] == []


async def test_many_clients_are_correct_with_bounded_concurrency(mock_db, meta):
    n = 30
    for i in range(n):
        await _client(mock_db, cid=f"g{i}", name=f"C{i}", targets={"cpl": 10})
        # even clients: CPL 20 (high); odd: CPL 10 (ok)
        meta[f"act_g{i}"] = {"cur": (20 * 2, 2), "prior": (40, 2)} if i % 2 == 0 else {"cur": (20, 2), "prior": (20, 2)}
    await mock_db["agent_cards"].insert_one(
        {"_id": "c", "user_id": "u1", "agent_id": "a", "client_group_id": "g1", "status": "open"})
    out = await _check(mock_db)
    assert len(out["clients"]) == n
    assert out["summary"] == {"high": 15, "medium": 1, "ok": 14}
    assert _one(out, "g1")["open_agent_cards"] == 1 and _one(out, "g1")["attention"] == "medium"
    assert meta["_state"]["peak"] <= account_check.MAX_CONCURRENCY == 8


async def test_tool_returns_the_check_and_validates_days(mock_db, meta, set_current_user):
    set_current_user("alice@example.com")
    await _client(mock_db, user="alice@example.com")
    meta["act_g1"] = {"cur": (50, 5), "prior": (50, 5)}
    out = await watch_mcp.check_my_accounts.fn(days=7)
    assert out["summary"] == {"high": 0, "medium": 0, "ok": 1}
    for bad in (0, 91, -3):
        with pytest.raises(ToolError, match="between 1 and 90"):
            await watch_mcp.check_my_accounts.fn(days=bad)


async def test_tool_requires_authentication(mock_db, no_current_user):
    with pytest.raises(ValueError, match="Unauthenticated"):
        await watch_mcp.check_my_accounts.fn()
