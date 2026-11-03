from services.creatives.verdict import (
    aggregate_days,
    derive,
    rollup,
    score_account,
    test_verdict as verdict_of,
)

T = {"mode": "thresholds", "min_spend": 150, "min_days": 7, "win_share": 0.6,
     "metrics": {"cpl": {"op": "lte", "value": 35}, "ctr": {"op": "gte", "value": 1.5},
                 "hook_rate": {"op": "gte", "value": 30}}}


def tot(**kw):
    base = dict(spend=0, impressions=0, clicks=0, link_clicks=0, leads=0, video_views_3s=0, days_live=0)
    return {**base, **kw}


def test_below_floor_is_scoring():
    assert score_account(tot(spend=149, days_live=6, leads=10, impressions=1000), T, fmt="video")["state"] == "scoring"


def test_floor_is_spend_or_days():
    assert score_account(tot(spend=20, days_live=7, leads=1, impressions=100, link_clicks=2, video_views_3s=40), T, fmt="video")["state"] != "scoring"
    assert score_account(tot(spend=150, days_live=1, leads=1, impressions=100), T, fmt="video")["state"] != "scoring"


def test_thresholds_pass_and_fail():
    ok = tot(spend=212, days_live=5, leads=11, impressions=10000, link_clicks=230, video_views_3s=4100)
    assert score_account(ok, T, fmt="video")["state"] == "pass"
    bad = {**ok, "leads": 3}
    s = score_account(bad, T, fmt="video")
    assert s["state"] == "fail" and [c["metric"] for c in s["checks"] if not c["ok"]] == ["cpl"]


def test_zero_leads_above_floor_fails_not_crashes():  # Review Focus 3
    s = score_account(tot(spend=300, days_live=8, impressions=5000, link_clicks=90, video_views_3s=2000), T, fmt="video")
    assert s["cpl"] is None and s["state"] == "fail"
    assert score_account(tot(spend=300, days_live=8), {**T, "mode": "cpl_only", "metrics": {"cpl": {"op": "lte", "value": 35}}}, fmt="image")["state"] == "fail"
    b = {**T, "mode": "baseline", "baseline_pct": 15, "metrics": {}}
    assert score_account(tot(spend=300, days_live=8), b, fmt="image", baseline={"cpl": 40.0, "ctr": 1.0})["state"] == "fail"


def test_hook_rate_ignored_for_images():
    ok = tot(spend=200, days_live=7, leads=10, impressions=10000, link_clicks=200)
    s = score_account(ok, T, fmt="image")
    assert s["state"] == "pass" and "hook_rate" not in [c["metric"] for c in s["checks"]]


def test_baseline_mode():
    b = {**T, "mode": "baseline", "baseline_pct": 15, "metrics": {}}
    acct = {"cpl": 40.0, "ctr": 1.0}
    assert score_account(tot(spend=200, days_live=7, leads=6, impressions=1000), b, fmt="image", baseline=acct)["state"] == "pass"  # cpl 33.3 <= 34
    assert score_account(tot(spend=200, days_live=7, leads=5, impressions=1000), b, fmt="image", baseline=acct)["state"] == "fail"  # cpl 40
    assert score_account(tot(spend=200, days_live=7, leads=5), b, fmt="image", baseline=None)["state"] == "scoring"


def test_baseline_with_ctr_enabled():
    b = {**T, "mode": "baseline", "baseline_pct": 15, "metrics": {"ctr": {"op": "gte", "value": 1.5}}}
    acct = {"cpl": 40.0, "ctr": 1.0}
    good = tot(spend=200, days_live=7, leads=6, impressions=1000, link_clicks=12)  # ctr 1.2 >= 1.15
    weak = tot(spend=200, days_live=7, leads=6, impressions=1000, link_clicks=10)  # ctr 1.0
    assert score_account(good, b, fmt="image", baseline=acct)["state"] == "pass"
    assert score_account(weak, b, fmt="image", baseline=acct)["state"] == "fail"


def test_cpl_only_mode():
    c = {**T, "mode": "cpl_only"}
    assert score_account(tot(spend=200, days_live=7, leads=6), c, fmt="image")["state"] == "pass"
    assert score_account(tot(spend=200, days_live=7, leads=5), c, fmt="image")["state"] == "fail"


def test_derive_handles_zero_denominators():
    d = derive(tot())
    assert d["cpl"] is None and d["ctr"] is None and d["cpc"] is None and d["hook_rate"] is None


def test_verdict_shares():
    s = lambda *st: [{"state": x} for x in st]
    assert verdict_of(s("pass", "scoring"), 0.6) == "testing"  # < 2 scored
    assert verdict_of(s("pass", "pass", "fail", "scoring"), 0.6) == "winner"
    assert verdict_of(s("fail", "fail", "pass"), 0.6) == "loser"
    assert verdict_of(s("pass", "fail"), 0.6) == "inconclusive"  # all scored, 50/50
    assert verdict_of(s("pass", "fail", "scoring"), 0.6) == "testing"


def test_leads_not_double_counted():
    days = [{"date": "2026-10-01", "spend": 10, "impressions": 100, "actions": [
        {"action_type": "lead", "value": "3"}, {"action_type": "onsite_conversion.lead_grouped", "value": "3"}]}]
    assert aggregate_days(days)["leads"] == 3


def test_aggregate_days_sums_and_days_live():
    days = [
        {"date": "2026-10-01", "spend": 10, "impressions": 100, "clicks": 5, "link_clicks": 4,
         "actions": [{"action_type": "lead", "value": "2"}, {"action_type": "video_view", "value": "30"}]},
        {"date": "2026-10-02", "spend": 0, "impressions": 0},
        {"date": "2026-10-03", "spend": 5, "impressions": 50, "actions": [{"action_type": "offsite_conversion.fb_pixel_lead", "value": "1"}]},
    ]
    t = aggregate_days(days)
    assert t == {"spend": 15, "impressions": 150, "clicks": 5, "link_clicks": 4, "leads": 3,
                 "video_views_3s": 30, "days_live": 2}


async def test_rollup_converts_mixed_currency(mock_db, monkeypatch):  # Review Focus 1
    from utils.currency_exchange import CurrencyService

    await mock_db["users"].insert_one({"user_id": "u1", "default_currency": "GBP"})
    monkeypatch.setattr(CurrencyService, "get_rate", staticmethod(lambda a, b: 0.75 if (a, b) == ("USD", "GBP") else 1.0))
    ads = [
        {"ad_account_id": "a1", "currency": "GBP", "format": "video",
         "score": {"state": "pass", "spend": 100, "leads": 4, "impressions": 1000, "link_clicks": 20, "video_views_3s": 300}},
        {"ad_account_id": "a2", "currency": "USD", "format": "video",
         "score": {"state": "fail", "spend": 100, "leads": 1, "impressions": 1000, "link_clicks": 10, "video_views_3s": 100}},
    ]
    s = await rollup(mock_db, "u1", ads)
    assert s["spend"] == 175.0 and s["currency"] == "GBP" and s["mixed_currency"] is True
    assert s["leads"] == 5 and s["cpl"] == 35.0
    assert s["ctr"] == 1.5 and s["hook_rate"] == 20.0
    assert (s["accounts_total"], s["accounts_scored"], s["accounts_pass"], s["accounts_fail"]) == (2, 2, 1, 1)
    # per-ad figures stay in the account currency
    assert ads[1]["score"]["spend"] == 100


async def test_rollup_rate_missing_excludes_money(mock_db, monkeypatch):
    from utils.currency_exchange import CurrencyService

    def boom(a, b):
        raise ValueError("no rate")

    await mock_db["users"].insert_one({"user_id": "u1", "default_currency": "GBP"})
    monkeypatch.setattr(CurrencyService, "get_rate", staticmethod(boom))
    ads = [
        {"ad_account_id": "a1", "currency": "GBP", "score": {"state": "pass", "spend": 100, "leads": 4}},
        {"ad_account_id": "a2", "currency": "USD", "score": {"state": "scoring", "spend": 50, "leads": 1}},
    ]
    s = await rollup(mock_db, "u1", ads)
    assert s["spend"] == 100.0 and s["rate_missing"] is True and s["mixed_currency"] is True


async def test_rollup_without_user_currency_never_guesses_one(mock_db):
    # No users.default_currency: no currency is assumed (never GBP) and no money is summed.
    s = await rollup(mock_db, "u1", [{"ad_account_id": "a", "currency": "USD",
                                      "score": {"state": "pass", "spend": 10, "leads": 1}}])
    assert s["currency"] is None and s["rate_missing"] is True
    assert s["spend"] == 0 and s["cpl"] is None


def test_none_targets_use_defaults_and_zero_is_valid():
    t = {"mode": "cpl_only", "min_spend": None, "min_days": None, "metrics": {"cpl": {"op": "lte", "value": 35}}}
    assert score_account(tot(spend=100, days_live=3, leads=5), t, fmt="image")["state"] == "scoring"
    t0 = {**t, "min_spend": 0}
    assert score_account(tot(spend=0, days_live=0, leads=0), t0, fmt="image")["state"] == "fail"
    assert verdict_of([{"state": "pass"}, {"state": "pass"}], None) == "winner"
