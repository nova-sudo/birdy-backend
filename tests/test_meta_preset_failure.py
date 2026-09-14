"""
tests/test_meta_preset_failure.py
---------------------------------
A broken preset fetch must not look like an empty account.

`_finalize_preset_result` packages an empty accumulator into `spend: 0` with
empty campaign lists — byte-identical to a preset that legitimately returned
nothing. So a transient HTTP error or an unexpected exception produced a
perfectly plausible zero, which was then written over the last good cache.

`cache_account_daily_spend` already states the rule for the daily series:
"None on failure, distinct from [], which legitimately means the account spent
nothing. Callers must not treat a failure as an empty history and overwrite good
data with it." These pin the same rule for presets.
"""

import httpx
import pytest

from services.meta_service import (
    _failed_result,
    _fetch_meta_campaigns_for_preset,
    _rate_limited_result,
)


def transport(status, body):
    def handler(_request):
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    def apply(status, body):
        real = httpx.AsyncClient

        def factory(*a, **kw):
            kw["transport"] = transport(status, body)
            return real(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
    return apply


class TestMarkerShape:
    def test_a_failure_is_flagged_and_carries_its_reason(self):
        r = _failed_result("last_7d", "HTTP 500: upstream exploded")
        assert r["_failed"] is True
        assert "upstream exploded" in r["_error"]

    def test_a_failure_is_distinguishable_from_a_rate_limit(self):
        """They need different handling: rate limits retry with backoff,
        failures just leave the cache alone."""
        assert _rate_limited_result("last_7d").get("_failed") is None
        assert _failed_result("last_7d", "boom").get("_rate_limited") is None

    def test_a_failure_still_has_the_shape_callers_expect(self):
        """Callers read result['metrics']['insights'] defensively; the marker
        must not turn a skip into an AttributeError."""
        r = _failed_result("last_7d", "boom")
        assert r["metrics"]["insights"] == {}
        assert r["campaigns"] == [] and r["ads"] == [] and r["adsets"] == []
        assert r["date_preset"] == "last_7d"


class TestFetch:
    @pytest.mark.asyncio
    async def test_a_server_error_reports_failure_not_zero_spend(self, patch_client):
        """The bug: this returned spend 0 with no marker, and the caller wrote
        it over real data."""
        patch_client(500, {"error": {"message": "internal"}})

        r = await _fetch_meta_campaigns_for_preset("act_1", "T", "last_7d")

        assert r.get("_failed") is True

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_account_is_not_flagged(self, patch_client):
        """Zero spend is a legitimate answer and must still be written —
        otherwise a client who stopped spending never updates."""
        patch_client(200, {"data": []})

        r = await _fetch_meta_campaigns_for_preset("act_1", "T", "last_7d")

        assert r.get("_failed") is None
        assert r["metrics"]["insights"]["spend"] == 0
        assert r["metrics"]["total_campaigns"] == 0

    @pytest.mark.asyncio
    async def test_a_rate_limit_keeps_its_own_marker(self, patch_client):
        patch_client(429, {"error": {"message": "rate limit", "code": 17}})

        r = await _fetch_meta_campaigns_for_preset("act_1", "T", "last_7d")

        assert r.get("_rate_limited") is True
        assert r.get("_failed") is None


# ---------------------------------------------------------------------------
# Creatives are fetched flat and joined by ad id
# ---------------------------------------------------------------------------
#
# The campaigns query used to expand creatives three levels deep —
# campaign → ads → creative → object_story_spec. Meta answered the whole
# request with HTTP 500 "Please reduce the amount of data you're asking for",
# on an account with 5 campaigns and 72 ads, for every date window. Creatives
# now come from one flat pass over /act_X/ads and join on ad id.

from services.meta_service import _accumulate_campaigns_page


def _one_campaign_page(ad_id="ad_1"):
    return [{
        "id": "camp_1",
        "name": "Campaign",
        "status": "ACTIVE",
        "insights": {"data": [{"spend": "10", "impressions": "100", "clicks": "5", "reach": "90"}]},
        "adsets": {"data": []},
        "ads": {"data": [{"id": ad_id, "name": "Ad", "adset_id": "as_1", "status": "ACTIVE"}]},
    }]


def _accumulate(page, creatives):
    campaigns, adsets, ads = [], [], []
    totals = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0}
    _accumulate_campaigns_page(page, campaigns, adsets, ads, totals, creatives)
    return ads


def test_an_ad_picks_up_its_creative_from_the_flat_map():
    ads = _accumulate(_one_campaign_page(), {
        "ad_1": {
            "creative_title": "Book now",
            "creative_body": "Limited slots",
            "creative_image": "https://example.test/a.jpg",
            "creative_thumbnail": "",
            "creative_video_id": "",
        }
    })
    assert ads[0]["creative_title"] == "Book now"
    assert ads[0]["creative_image"] == "https://example.test/a.jpg"


def test_an_ad_missing_from_the_creative_map_still_produces_a_row():
    """The creative fetch is best-effort: losing the pictures must not cost us
    the spend figures, which are what the report is actually for."""
    ads = _accumulate(_one_campaign_page(), {})
    assert ads[0]["id"] == "ad_1"
    assert ads[0]["spend"] == 0 or "spend" in ads[0]
    assert ads[0]["creative_title"] == ""
    assert ads[0]["creative_image"] == ""


def test_creatives_are_never_read_off_the_campaign_payload():
    """A stale nested `creative` in the response must not win over the flat
    map — otherwise re-introducing the nesting would silently half-work."""
    page = _one_campaign_page()
    page[0]["ads"]["data"][0]["creative"] = {"title": "from nesting"}
    ads = _accumulate(page, {"ad_1": {
        "creative_title": "from flat map", "creative_body": "",
        "creative_image": "", "creative_thumbnail": "", "creative_video_id": "",
    }})
    assert ads[0]["creative_title"] == "from flat map"
