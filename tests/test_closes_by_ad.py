"""
"What ad is generating all the closes?" — the three things that stopped Birdy
answering it on a client page:

  - the client page prompt never said today's date, so "last 30 days" became
    October 2023 and every window came back empty
  - ad rows carried full creative copy and image URLs (33k chars for 19 ads
    against an 8k budget), so every ad-level result was truncated
  - no tool joined closes to ads; get_closes_by_ad reads the Marketing Hub's
    opp rollup
"""

from datetime import date

from ai import orchestrator
from ai.config import MAX_RESULT_CHARS
from ai.tools import attribution_tools as at
from ai.tools.meta_tools import shape_cached_insights
from ai.tools.result_shaping import serialize_with_truncation

U = "u@example.com"
G = "g1"


def _lead(ad, status, value=0, adset="Set A", campaign="Camp A"):
    return {"ad_id": ad, "ad_name": f"Ad {ad}", "adset_id": "s1", "adset_name": adset,
            "campaign_id": "c1", "campaign_name": campaign, "ghl_matched": True,
            "ghl_opportunity_status": status, "ghl_opportunity_value": value}


async def test_closes_by_ad_ranks_ads_by_closes_with_cost_per_close(mock_db, monkeypatch):
    seen = {}

    async def fake_leads(user_id, group_ids, start, end, mongo_client):
        seen.update(user_id=user_id, group_ids=group_ids, start=start, end=end)
        return [_lead("a1", "open"), _lead("a2", "won", 300), _lead("a2", "won", 200),
                _lead("a2", "lost"), _lead("a1", "won", 100), _lead("a3", "")]

    async def fake_cache(db, user_id, level, group_ids, start, end):
        assert level == "ads"
        return [{"id": "a2", "spend": 100.0}, {"id": "a1", "spend": "50"}]

    monkeypatch.setattr(at, "fetch_ad_leads", fake_leads)
    monkeypatch.setattr(at, "_get_cached_data", fake_cache)

    out = await at.get_closes_by_ad(mock_db, U, mongo_client=object(), group_ids=[G], end_date="2026-10-27")
    assert seen == {"user_id": U, "group_ids": [G], "start": "2026-09-28", "end": "2026-10-27"}
    assert out["total_closes"] == 3 and out["total_leads"] == 6
    top = out["rows"][0]
    assert top == {"id": "a2", "name": "Ad a2", "adset_name": "Set A", "campaign_name": "Camp A",
                   "closes": 2, "revenue": 500.0, "leads": 3, "open": 0, "lost": 1,
                   "close_rate_pct": 66.7, "spend": 100.0, "cost_per_close": 50.0}
    assert [r["id"] for r in out["rows"]] == ["a2", "a1", "a3"]
    assert "cost_per_close" not in out["rows"][2] and "spend" not in out["rows"][2]


async def test_closes_by_ad_campaign_level_and_bad_input(mock_db, monkeypatch):
    async def fake_leads(*a, **k):
        return [_lead("a1", "won", 10), _lead("a2", "won", 20)]

    async def no_cache(*a, **k):
        return []

    monkeypatch.setattr(at, "fetch_ad_leads", fake_leads)
    monkeypatch.setattr(at, "_get_cached_data", no_cache)
    out = await at.get_closes_by_ad(mock_db, U, level="campaign", start_date="2026-10-01", end_date="2026-10-07")
    assert out["rows"] == [{"id": "c1", "name": "Camp A", "closes": 2, "revenue": 30.0, "leads": 2,
                            "open": 0, "lost": 0, "close_rate_pct": 100.0}]
    assert (await at.get_closes_by_ad(mock_db, U, level="creative"))["ok"] is False
    assert (await at.get_closes_by_ad(mock_db, U, end_date="27/10/2026"))["ok"] is False


def test_ad_rows_fit_the_budget_without_creative_and_sort_by_spend():
    creative = {"creative_body": "x" * 1500, "creative_image": "https://img/" + "y" * 400,
                "creative_thumbnail": "t", "creative_video_id": "v", "creative_title": "T" * 120}
    rows = [{"id": str(i), "name": f"Ad {i}", "spend": float(i), "cpl": 2.88551, "cost_per_result": 2.88551,
             "client_group_id": G, "client_group_name": "Hayley", **creative} for i in range(19)]
    shaped = shape_cached_insights(rows)
    assert len(serialize_with_truncation(shaped)) < MAX_RESULT_CHARS
    first = shaped["insights"][0]
    assert first["id"] == "18" and first["cpl"] == 2.89 and "cost_per_result" not in first
    assert not {"creative_body", "creative_image", "client_group_id"} & set(first)
    assert first["creative_title"].endswith("…") and len(first["creative_title"]) == 81
    assert shaped["client_group"] == {"id": G, "name": "Hayley"} and shaped["total"] == 19

    two = shape_cached_insights([{"id": "1", "client_group_id": "a"}, {"id": "2", "client_group_id": "b"}])
    assert "client_group" not in two and two["insights"][0]["client_group_id"] in ("a", "b")


def test_client_page_prompt_has_today_and_the_tool_is_offered_and_pinned():
    prompt = orchestrator._build_client_detail_prompt("Hayley", G)
    assert f"Today's date is {date.today().isoformat()}" in prompt
    assert "get_closes_by_ad" in prompt
    for page in ("client_detail", "campaigns", "leads", "opportunities", "dashboard"):
        assert at.TOOL_NAME in orchestrator._PAGE_TOOLS[page]
    assert at.TOOL_NAME in orchestrator._CLIENT_PINNED_GROUP_IDS_TOOLS
