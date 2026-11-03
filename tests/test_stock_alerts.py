from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from ai.agents.stock import alerts as alerts_mod
from ai.agents.stock.alerts import fetch_account_health, health_alerts, prepaid_estimate, run_alerts
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.store import AD_DAYS, AGENTS

NOW = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)  # last full day in London: 2026-11-02
GID = "g1"


def _health(**kw):
    return {"account_status": 1, "disable_reason": 0, "funding_source": "123", "is_prepay_account": False,
            "timezone_name": "Europe/London", "currency": "GBP", "name": "Acme", **kw}


def _ad(status="ACTIVE", ad_id="a1", **kw):
    return {"ad_id": ad_id, "name": f"Ad {ad_id}", "effective_status": status,
            "created_time": datetime(2026, 9, 1), **kw}


def _spend(**per_ad):
    """_spend(a1={"2026-11-02": 10}) -> the daily_spend shape: {ad_id: {iso_date: spend}}."""
    return per_ad


def _codes(alerts):
    return {a["code"] for a in alerts}


def _run(health=None, ads=None, spend=None, settings=None, **kw):
    return health_alerts(health or _health(), ads if ads is not None else [_ad()],
                         spend if spend is not None else {"a1": {"2026-11-01": 10, "2026-11-02": 10}},
                         settings or DEFAULT_SETTINGS, GID, NOW, **kw)


def test_healthy_account_has_no_alerts():
    assert _run() == []


def test_alert_shape():
    a = _run(health=_health(account_status=2, disable_reason=1))[0]
    assert set(a) == {"client_group_id", "code", "severity", "title", "detail", "since"}
    assert a["client_group_id"] == GID


def test_with_issues_is_not_called_rejected():
    ads = [_ad("WITH_ISSUES", issues=[{"error_summary": "Bad", "error_message": "Delivery error: pixel gone"}])]
    out = _run(ads=ads)
    assert "ad_with_issues" in _codes(out) and "ad_rejected" not in _codes(out)
    assert "Delivery error: pixel gone" in next(a for a in out if a["code"] == "ad_with_issues")["detail"]


def test_disapproved_is_rejected():
    ads = [_ad("DISAPPROVED", review_feedback={"global": {"reason": "Policy: text"}})]
    out = _run(ads=ads)
    a = next(a for a in out if a["code"] == "ad_rejected")
    assert "Ad a1" in a["detail"] and "Policy: text" in a["detail"]
    assert "ad_with_issues" not in _codes(out)


def test_pending_review_is_reported():
    assert "ad_in_review" in _codes(_run(ads=[_ad("PENDING_REVIEW")]))


@pytest.mark.parametrize("status", [3, 9])
def test_status_3_or_9_is_payment(status):
    assert "payment_issue" in _codes(_run(health=_health(account_status=status)))


def test_pending_billing_info_and_missing_funding_source_are_payment():
    assert "payment_issue" in _codes(_run(ads=[_ad("PENDING_BILLING_INFO")]))
    assert "payment_issue" in _codes(_run(health=_health(funding_source=None)))


def test_disabled_and_risk_review():
    assert "account_disabled" in _codes(_run(health=_health(account_status=2, disable_reason=3)))
    assert "risk_review" in _codes(_run(health=_health(account_status=7)))


def test_zero_spend_and_under_half():
    zero = _run(spend={"a1": {"2026-11-01": 10}})
    assert "spend_stopped" in _codes(zero)
    drop = _run(spend={"a1": {"2026-11-01": 10, "2026-11-02": 4}})
    assert "spend_drop" in _codes(drop) and "spend_stopped" not in _codes(drop)
    # exactly half is no alert
    assert "spend_drop" not in _codes(_run(spend={"a1": {"2026-11-01": 10, "2026-11-02": 5}}))


def test_spend_drop_ratio_comes_from_settings():
    s = {**DEFAULT_SETTINGS, "alerts": {**DEFAULT_SETTINGS["alerts"], "spend_drop_ratio": 0.8}}
    assert "spend_drop" in _codes(_run(spend={"a1": {"2026-11-01": 10, "2026-11-02": 7}}, settings=s))


def test_account_day_in_progress_is_not_judged():
    # Los Angeles: Nov 3 07:00 UTC is still Nov 2, so Nov 1 is the last full day. Nov 2 has no data yet.
    out = _run(health=_health(timezone_name="America/Los_Angeles"),
               spend={"a1": {"2026-10-31": 10, "2026-11-01": 9}})
    assert out == []


def test_active_ad_no_spend_two_days():
    spend = {"a1": {"2026-11-01": 10, "2026-11-02": 10}}
    ads = [_ad(), _ad(ad_id="a2")]
    out = _run(ads=ads, spend=spend)
    a = next(a for a in out if a["code"] == "ad_no_spend")
    assert "Ad a2" in a["detail"] and "Ad a1" not in a["detail"]


def test_new_ad_or_one_quiet_day_is_not_flagged():
    spend = {"a1": {"2026-11-01": 10, "2026-11-02": 10}, "a2": {"2026-11-01": 3}}
    fresh = _ad(ad_id="a3", created_time=datetime(2026, 11, 2, 12))
    out = _run(ads=[_ad(), _ad(ad_id="a2"), fresh], spend=spend)
    assert "ad_no_spend" not in _codes(out)


def test_paused_ad_with_no_spend_is_fine():
    assert "ad_no_spend" not in _codes(_run(ads=[_ad(), _ad("PAUSED", ad_id="a2")]))


# --- prepaid ---------------------------------------------------------------------------------

def test_prepaid_estimate_uses_budget_with_under_three_days():
    assert prepaid_estimate([{"date": "2026-11-01", "amount": 500}], 60, [20, 40], [30])["per_day"] == 30


def test_prepaid_estimate_uses_last_three_days_average():
    e = prepaid_estimate([{"date": "2026-10-01", "amount": 100}, {"date": "2026-10-20", "amount": 500}],
                         200, [20, 40, 30], [99])
    assert e == {"left": 300, "per_day": 30, "days_left": 10}


def test_prepaid_estimate_edges():
    assert prepaid_estimate([], 0, [], []) is None
    assert prepaid_estimate([{"date": "2026-11-01", "amount": 50}], 80, [10, 10, 10], [])["left"] == 0
    assert prepaid_estimate([{"date": "2026-11-01", "amount": 50}], 0, [], [])["days_left"] is None


def _prepaid_settings(amount=100, **alerts):
    s = {**DEFAULT_SETTINGS, "alerts": {**DEFAULT_SETTINGS["alerts"], **alerts}}
    s["clients"] = {GID: {"prepaid": {"topups": [{"date": "2026-10-28", "amount": amount}]}}}
    return s


def test_prepaid_alert_at_threshold_labelled_estimate():
    # topped up 100 on Oct 28; spent 4 days x 20 = 80; left 20 at 20/day: 1 day left
    spend = {"a1": {f"2026-10-{d}": 20 for d in (28, 29, 30, 31)} | {"2026-11-01": 20, "2026-11-02": 20}}
    out = _run(spend=spend, settings=_prepaid_settings(amount=200))  # left 80, 4 days at 20/day
    a = next(a for a in out if a["code"] == "prepaid_low")
    assert "estimate" in a["title"].lower() and "estimate" in a["detail"].lower()
    assert "4" in a["title"]


def test_prepaid_no_alert_above_threshold_or_without_topups():
    spend = {"a1": {"2026-11-01": 10, "2026-11-02": 10}}
    assert "prepaid_low" not in _codes(_run(spend=spend, settings=_prepaid_settings(amount=10000)))
    assert "prepaid_low" not in _codes(_run(spend=spend))


def test_prepaid_uses_budgets_when_topup_is_recent():
    s = _prepaid_settings(amount=100)
    s["clients"][GID]["prepaid"]["topups"][0]["date"] = "2026-11-02"
    out = _run(settings=s, daily_budgets=[40])  # left 90 (spent 10), 40/day budget: 2.25 days
    assert "prepaid_low" in _codes(out)


# --- fetch + run -----------------------------------------------------------------------------

@pytest.fixture
def graph(monkeypatch):
    state = {"calls": [], "accounts": {}, "fail": set()}

    async def fake_get(self, url, params=None, **kw):
        state["calls"].append((str(url), dict(params or {})))
        req = httpx.Request("GET", str(url))
        acct = str(url).rsplit("/", 1)[-1]
        if acct in state["fail"]:
            return httpx.Response(400, json={"error": {"code": 1, "message": "boom"}}, request=req)
        return httpx.Response(200, json=state["accounts"].get(acct, {
            "name": "Acme", "account_status": 1, "disable_reason": 0, "funding_source": "9",
            "is_prepay_account": True, "timezone_name": "Europe/London", "currency": "GBP"}), request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return state


async def test_fetch_account_health_shape(graph):
    h = await fetch_account_health("123", "tok")
    assert set(h) == {"account_status", "disable_reason", "funding_source", "is_prepay_account",
                      "timezone_name", "currency", "name"}
    url, params = graph["calls"][0]
    assert url.endswith("/act_123") and params["access_token"] == "tok"


async def test_fetch_account_health_error_raises(graph):
    graph["fail"].add("act_9")
    with pytest.raises(RuntimeError):
        await fetch_account_health("act_9", "tok")


async def _seed(db, gids=("g1", "g2"), account="act_1", settings=None):
    agent = {"_id": "ag1", "user_id": "u", "type": "stock_media_buying",
             "settings": settings or {**DEFAULT_SETTINGS, "clients": {}}}
    await db[AGENTS].insert_one(agent)
    for gid in gids:
        await db["client_groups"].insert_one({"id": gid, "user_id": "u", "name": f"Client {gid}",
                                              "meta_ad_account_id": account})
        await db["meta_ad_state"].insert_one({"client_group_id": gid, "ad_id": f"{gid}-a", "name": "Ad",
                                              "effective_status": "DISAPPROVED", "created_time": datetime(2026, 9, 1)})
    return agent


async def _token(user_id):
    return "tok"


async def test_shared_ad_account_read_once_alert_on_both_groups(mock_db, graph):
    agent = await _seed(mock_db)
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    health_calls = [c for c in graph["calls"] if c[0].endswith("/act_1")]
    assert len(health_calls) == 1
    assert {a["client_group_id"] for a in out if a["code"] == "ad_rejected"} == {"g1", "g2"}


async def test_read_failure_is_an_alert_not_an_exception(mock_db, graph):
    graph["fail"].add("act_1")
    agent = await _seed(mock_db)
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    assert [a["code"] for a in out] == ["check_failed", "check_failed"]
    assert {a["client_group_id"] for a in out} == {"g1", "g2"}
    assert "boom" in out[0]["detail"]
    assert len([c for c in graph["calls"] if c[0].endswith("/act_1")]) == 1


async def test_token_failure_is_an_alert(mock_db, graph):
    async def bad(user_id):
        raise RuntimeError("no token")
    agent = await _seed(mock_db, gids=("g1",))
    out = await run_alerts(mock_db, agent, bad, NOW, ["g1"])
    assert out[0]["code"] == "check_failed"


async def test_detected_prepaid_is_stored_only_when_unset(mock_db, graph):
    agent = await _seed(mock_db)
    agent["settings"]["clients"] = {"g2": {"prepaid": {"topups": [{"date": "2026-10-01", "amount": 5}]}}}
    await mock_db[AGENTS].update_one({"_id": "ag1"}, {"$set": {"settings": agent["settings"]}})
    await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    stored = (await mock_db[AGENTS].find_one({"_id": "ag1"}))["settings"]["clients"]
    assert stored["g1"]["prepaid"] == {"is_prepay_account": True, "topups": []}
    assert stored["g2"]["prepaid"] == {"topups": [{"date": "2026-10-01", "amount": 5}]}  # untouched


async def test_run_alerts_reads_spend_from_ad_days(mock_db, graph):
    agent = await _seed(mock_db, gids=("g1",))
    await mock_db["meta_ad_state"].update_many({}, {"$set": {"effective_status": "ACTIVE"}})
    await mock_db[AD_DAYS].insert_many([
        {"user_id": "u", "client_group_id": "g1", "ad_id": "g1-a", "date_start": "2026-11-01",
         "insight_data": {"spend": "10"}}])
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert "spend_stopped" in {a["code"] for a in out}
