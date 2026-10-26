from datetime import date, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.evaluators import REGISTRY
from ai.agents.evaluators import flag_declining, flag_no_spend
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import SEVERITY_MEDIUM

GID = "cg1"
AS_OF = date(2026, 10, 26)


def make_client(sym="£"):
    return ClientCtx(GID, "Acme", sym, 22.0)


def make_rule(kind, params=None, overrides=None):
    return {
        "_id": "r1", "agent_id": "a1", "kind": kind, "version": 1, "enabled": True, "mode": "flag",
        "params": {**DEFAULT_PARAMS[kind], **(params or {})}, "client_overrides": overrides or {},
    }


def day(offset, spend, leads, ad_id="a", campaign="c1", cname="Camp One"):
    return AdDay(ad_id, "as1", campaign, f"Ad {ad_id}", AS_OF - timedelta(days=offset),
                 float(spend), int(leads), cname)


def adset(status="ACTIVE", camp="ACTIVE", daily=50.0, life=0.0):
    return AdsetState("as1", "c1", "Set One", status, camp, daily, life)


def ad():
    return AdState("a", "as1", "c1", "Ad a", "ACTIVE", None, None)


def run_ns(rows, adsets=None, params=None):
    days = {"a": rows}
    return flag_no_spend.evaluate(make_rule("flag_no_spend", params), make_client(), days, {"a": ad()},
                                  {"as1": adset()} if adsets is None else adsets, AS_OF)


def run_dc(rows, params=None, sym="£"):
    days: dict = {}
    for r in rows:
        days.setdefault(r.ad_id, []).append(r)
    return flag_declining.evaluate(make_rule("flag_declining", params), make_client(sym), days,
                                   {"a": ad()}, {"as1": adset()}, AS_OF)


def test_registry():
    assert REGISTRY["flag_no_spend"] is flag_no_spend.evaluate
    assert REGISTRY["flag_declining"] is flag_declining.evaluate


def test_no_spend_fires_after_24h():
    rows = [day(5, 40, 1), day(3, 40, 1)]  # spent earlier, nothing in last 1 day
    out = run_ns(rows)
    assert len(out) == 1
    f = out[0]
    assert f.agent == "media_buying:flag_no_spend"
    assert f.action is None and f.near_miss is False
    assert f.severity == SEVERITY_MEDIUM
    assert f.title == "“Set One” spent £0 for 24 hrs"
    assert f.evidence.raw["adset_id"] == "as1"
    assert (f.agent_id, f.rule_id, f.rule_version) == ("a1", "r1", 1)


def test_no_spend_not_when_spent_today_or_48h_window():
    assert run_ns([day(5, 40, 1), day(0, 10, 0)]) == []
    rows = [day(5, 40, 1), day(1, 10, 0)]
    assert run_ns(rows) != []  # yesterday is outside the 1-day quiet window
    assert run_ns(rows, params={"hours": 48}) == []  # inside the 2-day window


def test_no_spend_hours_in_title():
    out = run_ns([day(10, 40, 1)], params={"hours": 48})
    assert out[0].title == "“Set One” spent £0 for 48 hrs"


def test_paused_campaign_skips():
    assert run_ns([day(5, 40, 1)], adsets={"as1": adset(camp="PAUSED")}) == []
    assert run_ns([day(5, 40, 1)], adsets={"as1": adset(status="PAUSED")}) == []


def test_zero_budget_skips():
    assert run_ns([day(5, 40, 1)], adsets={"as1": adset(daily=0.0)}) == []
    assert len(run_ns([day(5, 40, 1)], adsets={"as1": adset(daily=0.0, life=300.0)})) == 1


def test_never_spent_skips():
    assert run_ns([]) == []
    assert run_ns([day(5, 0, 0)]) == []
    assert run_ns([day(30, 40, 1)]) == []  # spend older than 14 days before the quiet window


def test_declining_fires_at_30pct():
    rows = [day(o, 10, 1) for o in range(7, 14)]  # prev: 70 / 7 = 10
    rows += [day(o, 13, 1) for o in range(0, 7)]  # now: 91 / 7 = 13
    out = run_dc(rows)
    assert len(out) == 1
    f = out[0]
    assert f.agent == "media_buying:flag_declining"
    assert f.action is None and f.severity == SEVERITY_MEDIUM
    assert f.title == "CPL up 30% week on week"
    assert f.description == "£10.00 → £13.00 · Camp One"


def test_declining_below_threshold():
    rows = [day(o, 10, 1) for o in range(7, 14)] + [day(o, 12.5, 1) for o in range(0, 7)]
    assert run_dc(rows) == []


def test_declining_needs_10_leads():
    rows = [day(o, 10, 1) for o in range(10, 14)] + [day(o, 20, 1) for o in range(0, 4)]
    assert run_dc(rows) == []  # 8 leads total


def test_declining_handles_zero_leads_prev_window():
    rows = [day(o, 10, 0) for o in range(7, 14)] + [day(o, 20, 2) for o in range(0, 7)]
    assert run_dc(rows) == []


def test_declining_zero_leads_now_no_crash():
    rows = [day(o, 10, 2) for o in range(7, 14)] + [day(o, 20, 0) for o in range(0, 7)]
    assert run_dc(rows) == []


def test_declining_falls_back_to_campaign_id_and_symbol():
    rows = [day(o, 10, 1, cname="") for o in range(7, 14)] + [day(o, 20, 1, cname="") for o in range(0, 7)]
    out = run_dc(rows, sym="$")
    assert out[0].description == "$10.00 → $20.00 · c1"
    assert out[0].title == "CPL up 100% week on week"


def test_declining_groups_by_campaign():
    rows = [day(o, 10, 1, ad_id="a", campaign="c1") for o in range(7, 14)]
    rows += [day(o, 20, 1, ad_id="a", campaign="c1") for o in range(0, 7)]
    rows += [day(o, 10, 1, ad_id="b", campaign="c2", cname="Two") for o in range(0, 14)]
    out = run_dc(rows)
    assert len(out) == 1 and "Camp One" in out[0].description
