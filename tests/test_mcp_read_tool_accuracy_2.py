"""Second pass over the MCP read tools (18 Nov 2026): values that were wrong or read as something they
were not. Each test names the tool and what it used to return."""

import pytest

from ai.tools import custom_metrics_tools, ghl_tools, group_tools, hp_tools, meta_tools, summary_tools
from integrations.facebook_utils.lead_fields import contact_fields

ALICE = "alice@example.com"


# ── get_ghl_tag_breakdown / get_account_summary: the preset was ignored ─────

async def _seed_tags(db):
    await db["client_groups"].insert_one({
        "id": "g1", "user_id": ALICE, "name": "Acme", "ghl_location_id": "loc1",
        "gohighlevel_cache": {"metrics": {"total_contacts": 3583,
                                          "tag_breakdown": {"live lead": 3066, "zombie lead": 2299}}},
        "facebook_cache": {"presets": {"last_30d": {"metrics": {"insights": {"spend": 10}}}}},
    })
    await db["client_group_tag_cache"].insert_many([
        {"group_id": "g1", "user_id": ALICE, "preset": "last_30d", "tags": {"live lead": 40, "booked": 7}},
        {"group_id": "g1", "user_id": ALICE, "preset": "maximum", "tags": {"live lead": 3066, "zombie lead": 2299}},
    ])


async def test_tag_breakdown_counts_the_window_asked_for(mock_db):
    await _seed_tags(mock_db)
    out = await ghl_tools.get_ghl_tag_breakdown(mock_db, ALICE, preset="last_30d")
    assert out["groups"][0]["top_tags"] == {"live lead": 40, "booked": 7}
    assert out["top_tags_across_all_groups"] == {"live lead": 40, "booked": 7}
    assert out["preset"] == "last_30d"
    lifetime = await ghl_tools.get_ghl_tag_breakdown(mock_db, ALICE, preset="maximum")
    assert lifetime["groups"][0]["top_tags"] == {"live lead": 3066, "zombie lead": 2299}


async def test_tag_breakdown_without_a_windowed_row_is_empty_not_lifetime(mock_db):
    await _seed_tags(mock_db)
    out = await ghl_tools.get_ghl_tag_breakdown(mock_db, ALICE, preset="last_7d")
    assert out["groups"][0]["top_tags"] == {}
    # Lifetime still works for a client refreshed before the windowed cache existed.
    await mock_db["client_group_tag_cache"].delete_many({"preset": "maximum"})
    lifetime = await ghl_tools.get_ghl_tag_breakdown(mock_db, ALICE, preset="maximum")
    assert lifetime["groups"][0]["top_tags"] == {"live lead": 3066, "zombie lead": 2299}


async def test_account_summary_top_tags_follow_the_preset(mock_db):
    await _seed_tags(mock_db)
    out = await summary_tools.get_account_summary(mock_db, ALICE, preset="last_30d")
    assert out["groups"][0]["ghl_top_tags"] == {"live lead": 40, "booked": 7}


# ── get_call_center_stats: a windowed count over an all-time lead count ─────

async def test_leads_called_rate_only_where_both_sides_cover_the_same_period(mock_db):
    stats = {"total_calls": 251, "answered_calls": 249, "leads_with_calls": 47, "total_leads": 915}
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": ALICE, "name": "Acme",
        "hotprospector_call_cache": {"last_7d": stats, "maximum": {**stats, "leads_with_calls": 761}},
    })
    week = (await hp_tools.get_call_center_stats(mock_db, ALICE, preset="last_7d"))["call_center"][0]
    assert week["leads_with_calls"] == 47
    assert week["total_leads_all_time"] == 915 and "total_leads" not in week
    assert week["leads_called_rate"] is None
    ever = (await hp_tools.get_call_center_stats(mock_db, ALICE, preset="maximum"))["call_center"][0]
    assert ever["leads_called_rate"] == 83.2


# ── get_client_groups: hotprospector_group_id is the old link, empty on every newer client ──

async def test_client_groups_say_whether_hotprospector_is_connected(mock_db):
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": ALICE, "name": "New link", "ghl_location_id": "loc1",
         "call_log_provider": "hotprospector"},
        {"id": "g2", "user_id": ALICE, "name": "Old link", "hotprospector_group_id": "hp9"},
        {"id": "g3", "user_id": ALICE, "name": "None", "ghl_location_id": "loc3"},
    ])
    out = await group_tools.get_client_groups(mock_db, ALICE)
    assert {g["id"]: g["hotprospector_connected"] for g in out["client_groups"]} == {
        "g1": True, "g2": True, "g3": False}


# ── get_facebook_leads: lead_id read from the wrong key; name and phone lost on custom form labels ──

async def test_facebook_leads_carry_their_id_name_and_phone(mock_db):
    await mock_db["facebook_leads"].insert_many([
        {"user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "lead_id": "111",
         "lead_data": {"lead_id": "111", "created_time": "2026-11-14T15:52:58+0000", "full_name": "",
                       "phone_number": "", "email": "a@x.com",
                       "field_data": {"first name": "Ann", "phone number": "+447000000001", "email": "a@x.com"}}},
        {"user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "lead_id": "222",
         "lead_data": {"lead_id": "222", "created_time": "2026-11-14T14:23:55+0000", "full_name": "Bea Bee",
                       "phone_number": "+447000000002", "email": "b@x.com",
                       "field_data": {"full_name": "Bea Bee", "phone_number": "+447000000002"}}},
    ])
    out = await meta_tools.get_facebook_leads(mock_db, ALICE)
    assert [(lead["lead_id"], lead["full_name"], lead["phone_number"]) for lead in out["leads"]] == [
        ("111", "Ann", "+447000000001"), ("222", "Bea Bee", "+447000000002")]


@pytest.mark.parametrize("answers, expected", [
    ({"full_name": "Bea Bee", "phone_number": "+44 1", "email": "b@x.com"}, ("Bea Bee", "b@x.com", "+44 1")),
    ({"first name": "Ann", "phone number": "+447000000002", "email": "a@x.com"}, ("Ann", "a@x.com", "+447000000002")),
    ({"First Name": "Cy", "Last name": "Dee", "Mobile": "+447000000003", "Email address": "c@x.com",
      "Best time to phone you": "after 5"},
     ("Cy Dee", "c@x.com", "+447000000003")),
    ({"what_treatment": "Fat freezing"}, ("", "", "")),
])
def test_lead_contact_fields_read_the_forms_own_labels(answers, expected):
    """Meta fills full_name / phone_number only when the form used its standard questions; a form that asks
    for "first name" and "phone number" leaves them out, and the phone never reached match_keys."""
    assert contact_fields(answers) == expected


# ── compute_custom_metric: a ratio rounded to 2dp, then read as a percentage ─

async def test_percentage_metric_is_returned_in_percent(mock_db, monkeypatch):
    await mock_db["users"].insert_one({"user_id": ALICE, "custom_metrics": [
        {"id": "custom_1", "name": "Close Rate", "format_type": "percentage", "aggregation": "total",
         "formula_parts": [{"type": "metric", "value": "ghl_won_opps"}, {"type": "operator", "value": "/"},
                           {"type": "metric", "value": "meta_leads"}]},
        {"id": "custom_2", "name": "CPA", "format_type": "currency", "aggregation": "average",
         "formula_parts": [{"type": "metric", "value": "meta_spend"}]},
    ]})
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": ALICE, "name": "Acme"})
    monkeypatch.setattr(custom_metrics_tools, "evaluate_formula", lambda *a, **k: 10 / 222)
    monkeypatch.setattr(custom_metrics_tools, "evaluate_formula_aggregated", lambda *a, **k: 10 / 222)
    rate = await custom_metrics_tools.compute_custom_metric(mock_db, ALICE, "custom_1", preset="last_30d")
    assert rate["per_group"][0]["value"] == 4.5 and rate["overall_total"] == 4.5
    assert rate["metric"]["unit"] == "percent"
    money = await custom_metrics_tools.compute_custom_metric(mock_db, ALICE, "custom_2", preset="last_30d")
    assert money["per_group"][0]["value"] == 0.05 and "unit" not in money["metric"]


# ── CPL: one definition, spend / Meta results ───────────────────────────────

def test_cpl_is_spend_over_meta_results_everywhere():
    """get_account_summary and compare_periods divided by the leads Birdy collected (54) while
    check_my_accounts and the insight tools divided by Meta's results (53): two CPLs for one client."""
    from ai.tools.derived_metrics import enrich
    row = enrich({"spend": 138.86, "clicks": 447, "results": 53, "total_leads": 54})
    assert row["cpl"] == row["cost_per_result"] == 2.62
    assert row["conversion_rate"] == 11.86
    # A row with no results figure at all still gets a CPL from the leads it has.
    assert enrich({"spend": 100, "total_leads": 4})["cpl"] == 25


def test_cpl_drivers_start_and_end_on_that_same_cpl():
    from ai.mcp.compare_mcp import _cpl_drivers
    a = {"spend": 708.87, "impressions": 58897, "clicks": 2632, "results": 296, "total_leads": 298}
    b = {"spend": 300.21, "impressions": 25306, "clicks": 872, "results": 102, "total_leads": 103}
    out = _cpl_drivers(a, b)
    assert (out["from"], out["to"]) == (round(708.87 / 296, 2), round(300.21 / 102, 2))


# ── The MCP tools themselves ────────────────────────────────────────────────
# The fixes above live in ai/tools; each MCP tool below was changed to call them. Calling only the
# shared function let a bad edit through: get_ghl_opportunity_stats was left calling the tag breakdown
# (NameError in production) while get_ghl_tag_breakdown kept its lifetime counts.

async def test_mcp_tools_reach_the_shared_functions(mock_db, set_current_user):
    from ai.mcp import custom_metrics_mcp, ghl_mcp, group_mcp, meta_mcp, summary_mcp
    set_current_user(ALICE)
    await _seed_tags(mock_db)
    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {
        "call_log_provider": "hotprospector",
        "ghl_opp_cache": {"last_30d": {"won": 10, "lost": 44, "open": 224, "total_opportunities": 278,
                                       "won_revenue": 3490}}}})
    await mock_db["facebook_leads"].insert_one({
        "user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "lead_id": "111",
        "lead_data": {"lead_id": "111", "created_time": "2026-11-14T15:52:58+0000",
                      "field_data": {"first name": "Ann", "phone number": "+447000000001"}}})
    await mock_db["users"].insert_one({"user_id": ALICE, "custom_metrics": [
        {"id": "custom_1", "name": "Close Rate", "format_type": "percentage", "formula_parts": []}]})

    opps = await ghl_mcp.get_ghl_opportunity_stats.fn(preset="last_30d", group_ids=["g1"])
    assert opps["overall"] == {"won": 10, "lost": 44, "open": 224, "abandoned": 0, "total_opportunities": 278,
                               "won_revenue": 3490.0, "conversion_rate": 3.6}

    tags = await ghl_mcp.get_ghl_tag_breakdown.fn(preset="last_30d", group_ids=["g1"], top_n=5)
    assert tags["groups"][0]["top_tags"] == {"live lead": 40, "booked": 7}

    summary = await summary_mcp.get_account_summary.fn(preset="last_30d")
    assert summary["groups"][0]["ghl_top_tags"] == {"live lead": 40, "booked": 7}

    groups = await group_mcp.get_client_groups.fn()
    assert groups["client_groups"][0]["hotprospector_connected"] is True

    leads = await meta_mcp.get_facebook_leads.fn(group_ids=["g1"])
    assert (leads["leads"][0]["lead_id"], leads["leads"][0]["full_name"]) == ("111", "Ann")

    metric = await custom_metrics_mcp.compute_custom_metric.fn(metric_id="custom_1", preset="last_30d")
    assert metric["metric"]["unit"] == "percent"
