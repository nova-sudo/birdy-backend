"""
tests/test_useless_ad_purger.py
-------------------------------
Unit tests for the deterministic detection in the useless_ad_purger subagent —
the logic a real-money pause decision rests on. Fully self-contained: uses a tiny
fake async DB (no mongo, no env, no LLM) so it runs anywhere.

Runnable via pytest OR directly: `python tests/test_useless_ad_purger.py`.
"""

import asyncio

from ai.suggestions.agents.useless_ad_purger import UselessAdPurger
from ai.suggestions.contracts import AnalyzerContext, ACTION_PAUSE_ADS, SEVERITY_HIGH


# --- tiny fake async DB (only .["alerts"].find(...).to_list() is used) --------

class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    async def to_list(self, length=None):
        return self._docs


class _FakeCollection:
    def __init__(self, docs):
        self._docs = list(docs)

    def find(self, *args, **kwargs):
        return _FakeCursor(self._docs)


class _FakeDB:
    def __init__(self, alerts=None):
        self._cols = {"alerts": _FakeCollection(alerts or [])}

    def __getitem__(self, name):
        return self._cols.get(name) or _FakeCollection([])


def _ad(id, name, status, spend, results):
    return {
        "id": id, "name": name, "status": status,
        "spend": spend, "results": results,
        "clicks": 50, "impressions": 5000, "reach": 3000,
    }


def _group(ads, currency="GBP"):
    return {
        "id": "grp1", "user_id": "u1", "name": "Palm Peach", "ad_account_currency": currency,
        "facebook_cache": {"last_7d": {"ads": ads}},
    }


def _run(alerts, group, window="weekly"):
    ctx = AnalyzerContext(db=_FakeDB(alerts=alerts), user_id="u1")
    return asyncio.run(UselessAdPurger().analyze(ctx, group, window))


# --- tests --------------------------------------------------------------------

def test_flags_zero_lead_and_over_baseline():
    """No alert configured → baseline = median CPL of converting ads × 1.75."""
    ads = [
        _ad("ad_zero", "Zero Lead Ad", "ACTIVE", 312, 0),   # waste: 0 leads, high spend → HIGH
        _ad("ad_exp", "Expensive Ad", "ACTIVE", 96, 2),     # cpl 48 >> baseline → HIGH
        _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),    # cpl 10
        _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),    # cpl 12
    ]
    findings = _run([], _group(ads))
    # One finding per offending ad (ad_zero: zero-lead; ad_exp: cpl 48 > baseline).
    assert len(findings) == 2, [f.title for f in findings]
    ids = {t["object_id"] for f in findings for t in f.action.targets}
    assert ids == {"ad_zero", "ad_exp"}, ids
    for f in findings:
        assert f.action.type == ACTION_PAUSE_ADS
        assert len(f.action.targets) == 1  # one ad per suggestion
        assert f.action.targets[0]["object_type"] == "ad"
        assert f.severity == SEVERITY_HIGH  # zero-lead, and cpl 48 > 21*1.5
        assert f.evidence.raw["target_source"] == "baseline"
        # baseline = median([10, 12, 48]) * 1.75 = 12 * 1.75 = 21.0
        assert f.evidence.raw["target"] == 21.0
    # The over-target ad shows a target stat labelled "Acct median" (baseline path).
    all_labels = {s.label for f in findings for s in f.evidence.stats}
    assert "Acct median" in all_labels
    print("PASS test_flags_zero_lead_and_over_baseline")


def test_uses_alert_threshold_as_target():
    """An existing cost_per_result ceiling alert becomes the target."""
    alert = {
        "user_id": "u1",
        "condition": {"metric": "cost_per_result", "operator": "gt", "value": 20.0},
        "status": "active",
        "target_group_ids": ["grp1"],
    }
    ads = [
        _ad("ad_exp", "Expensive Ad", "ACTIVE", 96, 2),   # cpl 48 > 20 → flagged
        _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),  # cpl 10 < 20
        _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),  # cpl 12 < 20
    ]
    findings = _run([alert], _group(ads))
    assert len(findings) == 1
    f = findings[0]
    assert {t["object_id"] for t in f.action.targets} == {"ad_exp"}
    assert f.evidence.raw["target_source"] == "alert"
    assert f.evidence.raw["target"] == 20.0
    labels = [s.label for s in f.evidence.stats]
    assert "Target" in labels
    print("PASS test_uses_alert_threshold_as_target")


def test_no_offenders_when_healthy():
    ads = [
        _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),  # cpl 10
        _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),  # cpl 12
    ]
    findings = _run([], _group(ads))
    assert findings == []
    print("PASS test_no_offenders_when_healthy")


def test_ignores_paused_and_low_spend():
    ads = [
        _ad("ad_paused", "Paused Waste", "PAUSED", 500, 0),  # paused → ignored
        _ad("ad_low", "Tiny Spend", "ACTIVE", 5, 0),         # below min spend → ignored
        _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),
        _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),
    ]
    findings = _run([], _group(ads))
    assert findings == [], f"expected no findings, got {[f.title for f in findings]}"
    print("PASS test_ignores_paused_and_low_spend")


def test_monthly_window_reads_last_30d():
    ads = [_ad("ad_zero", "Zero", "ACTIVE", 400, 0)]
    group = {
        "id": "grp1", "user_id": "u1", "name": "C", "ad_account_currency": "USD",
        "facebook_cache": {"last_30d": {"ads": ads}},  # only monthly preset present
    }
    weekly = _run([], group, "weekly")
    monthly = _run([], group, "monthly")
    assert weekly == []            # no last_7d data
    assert len(monthly) == 1       # last_30d picked up
    assert monthly[0].evidence.window == "monthly"
    print("PASS test_monthly_window_reads_last_30d")


if __name__ == "__main__":
    test_flags_zero_lead_and_over_baseline()
    test_uses_alert_threshold_as_target()
    test_no_offenders_when_healthy()
    test_ignores_paused_and_low_spend()
    test_monthly_window_reads_last_30d()
    print("\nAll useless_ad_purger tests passed.")
