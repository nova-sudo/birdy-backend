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


def _acct(**days):
    return {k.replace("_", "-"): v for k, v in days.items()}


ACCOUNT_SPEND = {f"2026-10-{d}": 20 for d in (28, 29, 30, 31)} | {"2026-11-01": 20, "2026-11-02": 20}


def test_prepaid_alert_at_threshold_labelled_estimate():
    # topped up 200 on Oct 28; account spent 6 days x 20 = 120; left 80 at 20/day: 4 days left
    out = _run(settings=_prepaid_settings(amount=200), account_spend=ACCOUNT_SPEND)
    a = next(a for a in out if a["code"] == "prepaid_low")
    assert "estimate" in a["title"].lower() and "estimate" in a["detail"].lower()
    assert "4" in a["title"]


def test_prepaid_uses_account_spend_not_the_groups_ads():
    # the group's own ads spent almost nothing; the account spend is what drains the top-up
    out = _run(spend={"a1": {"2026-11-01": 1, "2026-11-02": 1}}, settings=_prepaid_settings(amount=200),
               account_spend=ACCOUNT_SPEND)
    assert "prepaid_low" in _codes(out)


def test_prepaid_no_alert_above_threshold_or_without_topups_or_account_spend():
    assert "prepaid_low" not in _codes(_run(settings=_prepaid_settings(amount=10000), account_spend=ACCOUNT_SPEND))
    assert "prepaid_low" not in _codes(_run(account_spend=ACCOUNT_SPEND))
    assert "prepaid_low" not in _codes(_run(settings=_prepaid_settings(amount=200)))


def test_prepaid_uses_budgets_when_topup_is_recent():
    s = _prepaid_settings(amount=100)
    s["clients"][GID]["prepaid"]["topups"][0]["date"] = "2026-11-02"
    out = _run(settings=s, daily_budgets=[40], account_spend={"2026-11-02": 10})  # left 90, 40/day: 2.25 days
    assert "prepaid_low" in _codes(out)


def test_explicit_not_prepaid_gives_no_prepaid_alert():
    s = _prepaid_settings(amount=200)
    s["clients"][GID]["prepaid"]["is_prepay_account"] = False
    assert "prepaid_low" not in _codes(_run(settings=s, account_spend=ACCOUNT_SPEND))
    s["clients"][GID]["prepaid"]["is_prepay_account"] = True
    assert "prepaid_low" in _codes(_run(settings=s, account_spend=ACCOUNT_SPEND))


@pytest.mark.parametrize("topups", [[{"amount": 100}], [{"date": "not-a-date", "amount": 100}],
                                     [{"date": "2026-13-45", "amount": 100}], [{"date": None, "amount": 5}]])
def test_unusable_topups_never_raise_and_say_so(topups):
    s = {**DEFAULT_SETTINGS, "clients": {GID: {"prepaid": {"topups": topups}}}}
    out = _run(settings=s, account_spend=ACCOUNT_SPEND)
    assert "prepaid_low" not in _codes(out)
    a = next(a for a in out if a["code"] == "check_failed")
    assert "top-up" in a["detail"].lower()


def test_unusable_topups_are_ignored_beside_good_ones():
    s = _prepaid_settings(amount=200)
    s["clients"][GID]["prepaid"]["topups"].append({"date": "garbage", "amount": 1})
    out = _run(settings=s, account_spend=ACCOUNT_SPEND)
    assert "prepaid_low" in _codes(out) and "check_failed" not in _codes(out)


def test_missing_client_entry_or_clients_key_is_fine():
    assert _run(settings={**DEFAULT_SETTINGS, "clients": {"other": {}}}, account_spend=ACCOUNT_SPEND) == []
    s = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "clients"}
    assert _run(settings=s, account_spend=ACCOUNT_SPEND) == []


# --- campaign scope ---------------------------------------------------------------------------

def test_ad_level_alerts_only_consider_the_groups_campaigns():
    mine = _ad("DISAPPROVED", ad_id="m", campaign_id="c1")
    theirs = _ad("DISAPPROVED", ad_id="t", campaign_id="c2")
    quiet_theirs = _ad(ad_id="q", campaign_id="c2")
    out = _run(ads=[mine, theirs, quiet_theirs], spend={"x": {"2026-11-01": 5, "2026-11-02": 5}},
               campaign_ids=["c1"])
    rejected = next(a for a in out if a["code"] == "ad_rejected")
    assert "Ad m" in rejected["detail"] and "Ad t" not in rejected["detail"]
    assert "ad_no_spend" not in _codes(out)  # the sibling campaign's quiet ad is not this group's


def test_no_campaign_list_means_whole_account():
    out = _run(ads=[_ad("DISAPPROVED", campaign_id="c2")], campaign_ids=None)
    assert "ad_rejected" in _codes(out)


# --- spend data readiness ---------------------------------------------------------------------

def test_spend_alerts_skip_when_history_not_ready_or_late():
    stopped = {"a1": {"2026-11-01": 10}}
    for state in ("unready", "late"):
        out = _run(ads=[_ad(), _ad(ad_id="a2")], spend=stopped, history_state=state)
        assert not _codes(out) & {"spend_stopped", "spend_drop", "ad_no_spend"}
    assert "spend_data_late" in _codes(_run(spend=stopped, history_state="late"))
    assert "spend_data_late" not in _codes(_run(spend=stopped, history_state="unready"))
    late = next(a for a in _run(history_state="late") if a["code"] == "spend_data_late")
    assert late["severity"] == "info"


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


async def _seed(db, gids=("g1", "g2"), account="act_1", settings=None, newest_done="2026-11-03",
                history=True, campaigns=None):
    agent = {"_id": "ag1", "user_id": "u", "type": "stock_media_buying",
             "settings": settings or {**DEFAULT_SETTINGS, "clients": {}}}
    await db[AGENTS].insert_one(agent)
    for gid in gids:
        await db["client_groups"].insert_one({"id": gid, "user_id": "u", "name": f"Client {gid}",
                                              "meta_ad_account_id": account,
                                              **({"meta_campaign_ids": campaigns[gid]} if campaigns else {})})
        if history:
            await db["agent_ad_history"].insert_one({"_id": gid, "status": "ready", "newest_done": newest_done,
                                                     "last_topup_at": datetime(2026, 11, 3, 5)})
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


# --- run_alerts: account-level prepaid spend, history readiness, bad data -----------------------

@pytest.fixture
def insights(monkeypatch):
    """Fake account insights: alerts.fetch_account_spend."""
    state = {"calls": [], "spend": dict(ACCOUNT_SPEND), "error": None}

    async def fake(ad_account_id, token, since, until):
        state["calls"].append((ad_account_id, since, until))
        if state["error"]:
            raise RuntimeError(state["error"])
        return state["spend"]

    monkeypatch.setattr(alerts_mod, "fetch_account_spend", fake)
    return state


def _topup_settings(*gids, amount=200):
    return {**DEFAULT_SETTINGS, "clients": {g: {"prepaid": {"topups": [{"date": "2026-10-28", "amount": amount}]}}
                                            for g in gids}}


async def test_shared_account_prepaid_same_figures_on_both_groups(mock_db, graph, insights):
    agent = await _seed(mock_db, settings=_topup_settings("g1", "g2"))
    await mock_db["meta_ad_state"].delete_many({})
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    low = [a for a in out if a["code"] == "prepaid_low"]
    assert {a["client_group_id"] for a in low} == {"g1", "g2"}
    assert low[0]["title"] == low[1]["title"] and low[0]["detail"] == low[1]["detail"]
    assert len(insights["calls"]) == 1  # one account read for both groups
    assert insights["calls"][0][1] == date(2026, 10, 28)  # since the earliest top-up


async def test_no_account_spend_read_without_topups(mock_db, graph, insights):
    agent = await _seed(mock_db)
    await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    assert insights["calls"] == []


async def test_account_spend_failure_is_check_failed(mock_db, graph, insights):
    insights["error"] = "insights down"
    agent = await _seed(mock_db, gids=("g1",), settings=_topup_settings("g1"))
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    failed = [a for a in out if a["code"] == "check_failed"]
    assert len(failed) == 1 and "insights down" in failed[0]["detail"]


async def test_bad_topup_dates_do_not_raise_in_run(mock_db, graph, insights):
    s = {**DEFAULT_SETTINGS, "clients": {"g1": {"prepaid": {"topups": [{"date": "nope", "amount": 1}, {"amount": 2}]}}}}
    agent = await _seed(mock_db, gids=("g1",), settings=s)
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert any(a["code"] == "check_failed" and "top-up" in a["detail"].lower() for a in out)
    assert insights["calls"] == []


async def test_new_account_has_no_spend_false_alarms(mock_db, graph):
    agent = await _seed(mock_db, gids=("g1",), history=False)
    await mock_db["meta_ad_state"].update_many({}, {"$set": {"effective_status": "ACTIVE"}})
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert not {a["code"] for a in out} & {"spend_stopped", "spend_drop", "ad_no_spend", "spend_data_late"}


async def test_still_loading_history_has_no_false_alarms(mock_db, graph):
    agent = await _seed(mock_db, gids=("g1",))
    await mock_db["agent_ad_history"].update_one({"_id": "g1"}, {"$set": {"status": "running", "last_topup_at": None}})
    await mock_db["meta_ad_state"].update_many({}, {"$set": {"effective_status": "ACTIVE"}})
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert not {a["code"] for a in out} & {"spend_stopped", "ad_no_spend", "spend_data_late"}


async def test_stopped_account_still_alerts_when_history_is_current(mock_db, graph):
    # synced through Nov 3, last full day is Nov 2; Nov 2 has no row because Meta writes none for zero spend
    agent = await _seed(mock_db, gids=("g1",), newest_done="2026-11-03")
    await mock_db["meta_ad_state"].update_many({}, {"$set": {"effective_status": "ACTIVE"}})
    await mock_db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": "g1", "ad_id": "g1-a",
                                       "date_start": "2026-11-01", "insight_data": {"spend": "10"}})
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert "spend_stopped" in {a["code"] for a in out}


async def test_late_sync_gives_spend_data_late_not_spend_alerts(mock_db, graph):
    agent = await _seed(mock_db, gids=("g1",), newest_done="2026-10-30")
    await mock_db["meta_ad_state"].update_many({}, {"$set": {"effective_status": "ACTIVE"}})
    await mock_db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": "g1", "ad_id": "g1-a",
                                       "date_start": "2026-10-29", "insight_data": {"spend": "10"}})
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    codes = {a["code"] for a in out}
    assert "spend_data_late" in codes and not codes & {"spend_stopped", "spend_drop", "ad_no_spend"}


async def test_two_campaign_scoped_groups_on_one_account(mock_db, graph):
    agent = await _seed(mock_db, campaigns={"g1": ["c1"], "g2": ["c2"]})
    await mock_db["meta_ad_state"].delete_many({})
    await mock_db["meta_ad_state"].insert_many([
        {"client_group_id": g, "ad_id": f"{g}-{c}", "name": f"Ad {c}", "campaign_id": c,
         "effective_status": "DISAPPROVED", "created_time": datetime(2026, 9, 1)}
        for g in ("g1", "g2") for c in ("c1", "c2")])
    out = await run_alerts(mock_db, agent, _token, NOW, ["g1", "g2"])
    by_gid = {a["client_group_id"]: a["detail"] for a in out if a["code"] == "ad_rejected"}
    assert "Ad c1" in by_gid["g1"] and "Ad c2" not in by_gid["g1"]
    assert "Ad c2" in by_gid["g2"] and "Ad c1" not in by_gid["g2"]


async def test_prefill_with_missing_clients_key_or_gid(mock_db, graph):
    s = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "clients"}
    agent = await _seed(mock_db, gids=("g1",), settings=s)
    await run_alerts(mock_db, agent, _token, NOW, ["g1"])
    assert (await mock_db[AGENTS].find_one({"_id": "ag1"}))["settings"]["clients"]["g1"]["prepaid"]["is_prepay_account"] is True
