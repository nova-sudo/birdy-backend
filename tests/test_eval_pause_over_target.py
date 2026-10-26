from datetime import date, datetime, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.evaluators import REGISTRY
from ai.agents.evaluators.pause_over_target import evaluate
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import ACTION_PAUSE_ADS
from ai.suggestions.store import build_suggestion_doc

GID = "cg1"
START = date(2026, 10, 1)


def make_days(ad_id, rows, start=START, adset_id="as1"):
    return [
        AdDay(ad_id, adset_id, "c1", f"Ad {ad_id}", start + timedelta(days=i), float(s), int(l))
        for i, (s, l) in enumerate(rows)
    ]


def make_ad(ad_id, status="ACTIVE", adset_id="as1", created=None):
    return AdState(ad_id, adset_id, "c1", f"Ad {ad_id}", status, created, None)


def make_adset(adset_id="as1", name="Set One"):
    return AdsetState(adset_id, "c1", name, "ACTIVE", "ACTIVE", 50.0, 0.0)


def make_client(target=22.0, sym="£"):
    return ClientCtx(GID, "Acme", sym, target)


def make_rule(overrides=None, params=None):
    return {
        "_id": "r1", "agent_id": "a1", "kind": "pause_over_target", "version": 3,
        "enabled": True, "mode": "ask",
        "params": {**DEFAULT_PARAMS["pause_over_target"], **(params or {})},
        "client_overrides": overrides or {},
    }


def run(days_by_ad, ads, rule=None, client=None, as_of=None, adsets=None, near_miss=False):
    if as_of is None:
        as_of = max(d.day for ds in days_by_ad.values() for d in ds)
    return evaluate(rule or make_rule(), client or make_client(), days_by_ad, ads,
                    adsets if adsets is not None else {"as1": make_adset()}, as_of, near_miss=near_miss)


def test_registry():
    assert REGISTRY["pause_over_target"] is evaluate


def test_fires_over_multiple():
    days = {"a": make_days("a", [(20, 0), (20, 0), (20, 1), (20, 0)])}
    out = run(days, {"a": make_ad("a")})
    assert len(out) == 1
    f = out[0]
    assert f.agent == "media_buying:pause_over_target"
    assert (f.agent_id, f.rule_id, f.rule_version) == ("a1", "r1", 3)
    assert f.near_miss is False
    assert f.action.type == ACTION_PAUSE_ADS
    assert f.action.targets == [{"object_id": "a", "object_type": "ad", "name": "Ad a"}]
    assert f.title == "Pause “Ad a”"
    assert f.description == "£80 CPL vs £22 target · 1 leads in 4 days · £80 spent"
    assert len(f.evidence.stats) >= 4


def test_waits_for_patience():
    days = {"a": make_days("a", [(25, 0), (25, 0)])}
    assert run(days, {"a": make_ad("a")}) == []
    nm = run(days, {"a": make_ad("a")}, near_miss=True)
    assert len(nm) == 1 and nm[0].near_miss is True and nm[0].action is None


def test_zero_leads_after_80():
    days = {"a": make_days("a", [(42.5, 0), (42.5, 0)])}
    out = run(days, {"a": make_ad("a")}, client=make_client(target=None))
    assert len(out) == 1 and out[0].action is not None


def test_no_target_only_zero_lead_branch():
    days = {"a": make_days("a", [(30, 1)] * 5)}
    assert run(days, {"a": make_ad("a")}, client=make_client(target=None)) == []


def test_uses_last_7_days_when_older():
    rows = [(10, 2)] * 10 + [(20, 0)] * 6 + [(20, 1)]
    days = {"a": make_days("a", rows)}
    # lifetime CPL = 340/21 ~ 16 (fine); last 7 = 140/1 = 140 > 33
    out = run(days, {"a": make_ad("a")})
    assert len(out) == 1
    assert "in 7 days" in out[0].description


def test_old_ad_uses_created_time_for_age():
    # only 3 spend rows but created long ago -> last-7-days window, not lifetime
    days = {"a": make_days("a", [(30, 0), (30, 1), (30, 0)], start=date(2026, 10, 20))}
    ad = make_ad("a", created=datetime(2026, 9, 1))
    out = run(days, {"a": ad})
    assert len(out) == 1  # 90/1 = 90 > 33 either way; window is last 7
    assert out[0].evidence.raw["window_kind"] == "last_7d"


def test_respects_client_override():
    rows = [(20, 1)] * 4  # CPL 20, target 14.3 -> 1.4x
    days = {"a": make_days("a", rows)}
    client = make_client(target=100 / 7)
    assert run(days, {"a": make_ad("a")}, client=client) == []
    rule = make_rule(overrides={GID: {"cpl_multiple": 1.3}})
    assert len(run(days, {"a": make_ad("a")}, rule=rule, client=client)) == 1


def test_skips_paused_ads():
    days = {"a": make_days("a", [(20, 0)] * 4)}
    assert run(days, {"a": make_ad("a", status="PAUSED")}) == []


def test_adset_rollup_when_all_ads_fail():
    days = {"a": make_days("a", [(20, 0)] * 4), "b": make_days("b", [(25, 0)] * 4)}
    ads = {"a": make_ad("a"), "b": make_ad("b")}
    out = run(days, ads)
    assert len(out) == 1
    t = out[0].action.targets
    assert t == [{"object_id": "as1", "object_type": "adset", "name": "Set One"}]


def test_no_rollup_when_a_sibling_is_fine():
    days = {"a": make_days("a", [(20, 0)] * 4), "b": make_days("b", [(20, 3)] * 4)}
    out = run(days, {"a": make_ad("a"), "b": make_ad("b")})
    assert len(out) == 1 and out[0].action.targets[0]["object_type"] == "ad"


def test_no_rollup_with_single_active_ad_and_paused_sibling_ignored():
    days = {"a": make_days("a", [(20, 0)] * 4)}
    ads = {"a": make_ad("a"), "b": make_ad("b", status="PAUSED")}
    out = run(days, ads)
    assert out[0].action.targets[0]["object_type"] == "ad"


def test_currency_symbol_in_copy():
    days = {"a": make_days("a", [(20, 0), (20, 0), (20, 1), (20, 0)])}
    out = run(days, {"a": make_ad("a")}, client=make_client(sym="$"))
    assert "$" in out[0].description and "£" not in out[0].description


def test_cpl_near_miss():
    # threshold 33; CPL 30 is within 15%
    days = {"a": make_days("a", [(30, 1)] * 4)}
    assert run(days, {"a": make_ad("a")}) == []
    nm = run(days, {"a": make_ad("a")}, near_miss=True)
    assert len(nm) == 1 and nm[0].near_miss and nm[0].action is None


def test_zero_lead_near_miss():
    days = {"a": make_days("a", [(35, 0), (35, 0)])}  # 70 >= 68, < 80
    nm = run(days, {"a": make_ad("a")}, client=make_client(target=None), near_miss=True)
    assert len(nm) == 1 and nm[0].near_miss


def test_no_near_miss_flag_by_default_and_fire_not_duplicated():
    days = {"a": make_days("a", [(20, 0)] * 4)}
    out = run(days, {"a": make_ad("a")}, near_miss=True)
    assert len(out) == 1 and out[0].near_miss is False


def test_suggestion_doc_copies_agent_fields():
    days = {"a": make_days("a", [(20, 0)] * 4)}
    f = run(days, {"a": make_ad("a")})[0]
    doc = build_suggestion_doc("u1", f)
    assert doc["agent_id"] == "a1" and doc["rule_id"] == "r1" and doc["rule_version"] == 3
    f2 = run(days, {"a": make_ad("a")})[0]
    f2.agent_id = f2.rule_id = ""; f2.rule_version = 0
    assert "agent_id" not in build_suggestion_doc("u1", f2)
