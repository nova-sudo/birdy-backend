"""
tests/test_meta_account_totals.py
---------------------------------
Preset headline totals come from the account, not from summing campaigns.

`/{account}/campaigns` omits deleted and archived campaigns, so spend on them
vanished from the preset headline while `meta_daily_spend` — which asks the
account-level insights edge — still counted it. The gap scaled with how much of
an account's history sat on campaigns Meta no longer lists:

    campaigns returned    preset headline    real account spend
                     0            GBP 0.00           GBP 4,532
                     1              150.16               1,573
                     8            1,162.78               2,445
                    26            1,436.52               2,151
                   338           22,175.22   (control, reconciles)

These pin the fix and, just as importantly, the fallback: if the account edge
gives us nothing we keep the campaign sum rather than replacing a real number
with a zero.
"""

import pytest

from services.meta_service import (
    _finalize_preset_result,
    _totals_from_account_insights,
)


def account_body(spend="4532.08", impressions="100000", clicks="2000", reach="50000"):
    return {"data": [{
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "reach": reach,
        "actions": [{"action_type": "lead", "value": "42"}],
    }]}


class TestParsing:
    def test_an_account_row_becomes_totals(self):
        t = _totals_from_account_insights(account_body())
        assert t["spend"] == 4532.08
        assert t["impressions"] == 100000
        assert t["clicks"] == 2000
        assert t["reach"] == 50000
        assert t["results"] == 42

    def test_no_row_is_none_not_zero(self):
        """None means 'no account figure'. Zero would overwrite the campaign
        sum with a number we never actually measured."""
        assert _totals_from_account_insights({"data": []}) is None
        assert _totals_from_account_insights({}) is None
        assert _totals_from_account_insights(None) is None

    def test_an_unparseable_row_is_none(self):
        assert _totals_from_account_insights({"data": [{"spend": "not-a-number"}]}) is None


class TestHeadline:
    def test_the_headline_uses_account_totals_over_the_campaign_sum(self):
        """The actual bug: zero campaigns returned, GBP 4,532 really spent."""
        account = _totals_from_account_insights(account_body())
        campaign_sum = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0}

        r = _finalize_preset_result("maximum", [], [], [], account or campaign_sum)

        assert r["metrics"]["insights"]["spend"] == 4532.08

    def test_the_drill_down_lists_are_untouched(self):
        """Campaigns are the drill-down and are allowed to account for less
        than the whole — only the headline is corrected."""
        account = _totals_from_account_insights(account_body())
        campaigns = [{"id": "c1", "name": "Live campaign", "spend": 100.0}]

        r = _finalize_preset_result("maximum", campaigns, [], [], account)

        assert r["campaigns"] == campaigns
        assert r["metrics"]["total_campaigns"] == 1
        assert r["metrics"]["insights"]["spend"] == 4532.08

    def test_a_missing_account_row_falls_back_to_the_campaign_sum(self):
        """No account figure must never be worse than the old behaviour."""
        account = _totals_from_account_insights({"data": []})
        campaign_sum = {"spend": 250.0, "impressions": 10, "clicks": 5, "reach": 8, "results": 2}

        r = _finalize_preset_result("last_7d", [], [], [], account or campaign_sum)

        assert r["metrics"]["insights"]["spend"] == 250.0

    def test_derived_rates_are_computed_from_the_account_figures(self):
        """cpm/cpc/ctr must follow the numerator they are quoted against, or
        the headline and its rates disagree."""
        account = _totals_from_account_insights(
            account_body(spend="1000", impressions="100000", clicks="1000")
        )

        ins = _finalize_preset_result("last_7d", [], [], [], account)["metrics"]["insights"]

        assert ins["cpm"] == 10.0     # 1000 / 100000 * 1000
        assert ins["cpc"] == 1.0      # 1000 / 1000
        assert ins["ctr"] == 1.0      # 1000 / 100000 * 100
