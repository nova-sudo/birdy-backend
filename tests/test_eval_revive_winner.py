from datetime import date, datetime, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.evaluators import REGISTRY
from ai.agents.evaluators.revive_winner import evaluate
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import ACTION_ENABLE_ADS, SEVERITY_OPPORTUNITY

GID = "cg1"
AS_OF = date(2026, 10, 26)


def make_days(ad_id, rows, start=date(2026, 6, 1), adset_id="as1"):
    return [
        AdDay(ad_id, adset_id, "c1", f"Ad {ad_id}", start + timedelta(days=i), float(s), int(l))
        for i, (s, l) in enumerate(rows)
    ]


def make_ad(ad_id="a", status="PAUSED", paused_days=40, created_days=60, adset_id="as1"):
    created = datetime.combine(AS_OF - timedelta(days=created_days), datetime.min.time()) if created_days is not None else None
    paused = AS_OF - timedelta(days=paused_days) if paused_days is not None else None
    return AdState(ad_id, adset_id, "c1", f"Ad {ad_id}", status, created, paused)


def make_adset(status="ACTIVE", camp="ACTIVE"):
    return AdsetState("as1", "c1", "Set One", status, camp, 50.0, 0.0)


def make_client(target=22.0, sym="£"):
    return ClientCtx(GID, "Acme", sym, target)


def make_rule(params=None, overrides=None):
    return {
        "_id": "r1", "agent_id": "a1", "kind": "revive_winner", "version": 2,
        "enabled": True, "mode": "ask",
        "params": {**DEFAULT_PARAMS["revive_winner"], **(params or {})},
        "client_overrides": overrides or {},
    }


GOOD = [(100, 6)]  # CPL 16.67, 6 leads


def run(ad=None, rows=GOOD, adsets=None, client=None, rule=None, near_miss=False):
    ad = ad or make_ad()
    days = {ad.ad_id: make_days(ad.ad_id, rows)}
    return evaluate(rule or make_rule(), client or make_client(), days, {ad.ad_id: ad},
                    {"as1": make_adset()} if adsets is None else adsets, AS_OF, near_miss=near_miss)


def test_registry():
    assert REGISTRY["revive_winner"] is evaluate


def test_fires_for_old_winner():
    out = run()
    assert len(out) == 1
    f = out[0]
    assert f.agent == "media_buying:revive_winner"
    assert (f.agent_id, f.rule_id, f.rule_version) == ("a1", "r1", 2)
    assert f.severity == SEVERITY_OPPORTUNITY
    assert f.near_miss is False
    assert f.action.type == ACTION_ENABLE_ADS
    assert f.action.targets == [{"object_id": "a", "object_type": "ad", "name": "Ad a"}]
    assert f.title == "Turn back on “Ad a”"
    assert f.description == "Paused 40 days ago at £17 CPL vs £22 target · creative 60 days old"


def test_currency_symbol():
    out = run(client=make_client(sym="$"))
    assert "$17 CPL" in out[0].description


def test_too_recent():
    assert run(make_ad(paused_days=10)) == []


def test_creative_too_old():
    assert run(make_ad(created_days=120)) == []


def test_parent_paused_skips():
    assert run(adsets={"as1": make_adset(status="PAUSED")}) == []
    assert run(adsets={"as1": make_adset(status="CAMPAIGN_PAUSED")}) == []
    assert run(adsets={"as1": make_adset(camp="PAUSED")}) == []
    assert run(adsets={}) == []


def test_not_enough_leads():
    assert run(rows=[(40, 3)]) == []


def test_no_target_skips():
    assert run(client=make_client(target=None)) == []
    assert run(client=make_client(target=None), near_miss=True) == []


def test_skips_active_and_unpaused_at_and_no_created():
    assert run(make_ad(status="ACTIVE")) == []
    assert run(make_ad(paused_days=None)) == []
    assert run(make_ad(created_days=None)) == []


def test_cpl_over_target_not_fired():
    assert run(rows=[(200, 6)]) == []  # CPL 33


def test_lifetime_ignores_rows_after_as_of():
    ad = make_ad()
    days = {"a": make_days("a", [(100, 6)]) + [AdDay("a", "as1", "c1", "Ad a", AS_OF + timedelta(days=1), 999.0, 0)]}
    out = evaluate(make_rule(), make_client(), days, {"a": ad}, {"as1": make_adset()}, AS_OF)
    assert len(out) == 1


def test_near_miss_creative_age_88_days():
    out = run(make_ad(created_days=85), rule=make_rule({"max_creative_age_days": 90}))
    assert len(out) == 1 and out[0].near_miss is False
    rule = make_rule({"max_creative_age_days": 88})
    assert run(make_ad(created_days=89), rule=rule) == []
    nm = run(make_ad(created_days=89), rule=rule, near_miss=True)
    assert len(nm) == 1 and nm[0].near_miss is True and nm[0].action is None
    assert run(make_ad(created_days=92), rule=rule, near_miss=True) == []


def test_near_miss_cpl_within_15pct():
    nm = run(rows=[(125, 5)], near_miss=True)  # CPL 25 = 1.136x
    assert len(nm) == 1 and nm[0].near_miss and nm[0].action is None
    assert run(rows=[(125, 5)]) == []
    assert run(rows=[(150, 5)], near_miss=True) == []  # CPL 30 = 1.36x


def test_near_miss_paused_days_and_leads():
    nm = run(make_ad(paused_days=28), near_miss=True)
    assert len(nm) == 1 and nm[0].near_miss
    assert run(make_ad(paused_days=26), near_miss=True) == []
    nm = run(rows=[(80, 4)], near_miss=True)  # 1 lead short, CPL 20
    assert len(nm) == 1 and nm[0].near_miss
    assert run(rows=[(60, 3)], near_miss=True) == []  # 2 short


def test_two_short_is_not_near_miss():
    assert run(make_ad(paused_days=28), rows=[(125, 5)], near_miss=True) == []


def test_fire_not_duplicated_with_near_miss():
    out = run(near_miss=True)
    assert len(out) == 1 and out[0].near_miss is False
