"""Read tools that returned wrong or misleading figures (found Nov 2026 by calling every read tool
and checking the odd ones against Meta, GoHighLevel and HotProspector directly)."""

from datetime import date, datetime, timedelta

import pytest

from ai.agents import confidence
from ai.mcp.client_mcp import hotprospector_connected
from ai.tools import alert_tools, hp_tools, summary_tools, unified_leads_tools
from ai.tools.meta_tools import shape_cached_insights
from services import alert_history, client_refresh
from services.hp_service import incremental_lookback_days
from services.meta_service import _campaigns_fields_for_preset
from services.report_template import sources_connected

ALICE, BOB = "alice@example.com", "bob@example.com"


# ── unified leads ───────────────────────────────────────────────────────────

async def _seed_leads(db):
    await db["client_groups"].insert_many([
        {"id": "g1", "user_id": ALICE, "name": "Acme", "ghl_location_id": "loc1"},
        {"id": "g2", "user_id": ALICE, "name": "Other", "ghl_location_id": "loc2"},
    ])
    await db["ghl_contacts"].insert_many([
        {"user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "contact_id": "c1",
         "match_keys": ["email:a@x.com"], "contact_data": {"firstName": "Ann", "dateAdded": "2026-11-02T10:00:00Z"}},
        {"user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "contact_id": "c2",
         "match_keys": ["email:b@x.com"], "contact_data": {"firstName": "Bea", "dateAdded": "2026-11-01T10:00:00Z"}},
        {"user_id": ALICE, "client_group_id": "g1", "client_group_name": "Acme", "contact_id": "c3",
         "match_keys": [], "contact_data": {"firstName": "Cy", "dateAdded": "2026-10-30T10:00:00Z"}},
        {"user_id": ALICE, "client_group_id": "g2", "client_group_name": "Other", "contact_id": "c4",
         "match_keys": ["email:z@x.com"], "contact_data": {"firstName": "Zed", "dateAdded": "2026-11-02T10:00:00Z"}},
    ])
    await db["facebook_leads"].insert_many([
        # The sync stores the campaign and ad names under lead_data, not at the top level.
        {"user_id": ALICE, "client_group_id": "g1", "lead_id": "m1", "match_keys": ["email:a@x.com"],
         "lead_data": {"campaign_name": "Spring CBO", "ad_name": "Evergreen"}},
        {"user_id": ALICE, "client_group_id": "g2", "lead_id": "m2", "match_keys": ["email:z@x.com"], "lead_data": {}},
        {"user_id": ALICE, "client_group_id": "g2", "lead_id": "m3", "match_keys": ["email:q@x.com"], "lead_data": {}},
    ])
    await db["hotprospector_leads"].insert_many([
        {"user_id": ALICE, "ghl_location_id": "loc1", "match_keys": ["email:a@x.com"],
         "lead_data": {"id": 1, "call_logs_count": 2}},
        {"user_id": ALICE, "ghl_location_id": "loc1", "match_keys": ["email:b@x.com"], "lead_data": {"id": 2}},
        {"user_id": ALICE, "ghl_location_id": "loc2", "match_keys": ["email:z@x.com"], "lead_data": {"id": 3}},
    ])


async def test_lead_stats_totals_follow_the_client_filter(mock_db):
    await _seed_leads(mock_db)
    one = await unified_leads_tools.get_unified_lead_stats(mock_db, ALICE, group_ids=["g1"])
    assert one["totals"] == {"ghl_contacts": 3, "meta_leads": 1, "hotprospector_leads": 2}
    assert one["overlap_in_sample"] == {"ghl_only": 1, "ghl_and_meta": 0, "ghl_and_hp": 1,
                                        "ghl_and_meta_and_hp": 1, "sample_size": 3}
    everyone = await unified_leads_tools.get_unified_lead_stats(mock_db, ALICE)
    assert everyone["totals"] == {"ghl_contacts": 4, "meta_leads": 3, "hotprospector_leads": 3}


async def test_unified_leads_carry_the_meta_campaign_and_ad(mock_db):
    await _seed_leads(mock_db)
    out = await unified_leads_tools.get_unified_leads(mock_db, ALICE, group_ids=["g1"])
    ann = next(lead for lead in out["leads"] if lead["contact_id"] == "c1")
    assert ann["sources"] == ["ghl", "meta", "hp"]
    assert (ann["meta_campaign"], ann["meta_ad"]) == ("Spring CBO", "Evergreen")
    assert ann["hp_call_count"] == 2
    cy = next(lead for lead in out["leads"] if lead["contact_id"] == "c3")
    assert (cy["sources"], cy["hp_call_count"]) == (["ghl"], 0)


# ── HotProspector incremental lookback ──────────────────────────────────────

def _calls(*days):
    return [{"call_time_iso": f"{d}T10:00:00"} for d in days]


def test_lookback_stays_short_when_nothing_was_missed():
    today = date(2026, 11, 18)
    stored = _calls("2026-11-13", "2026-11-14", "2026-11-16", "2026-11-17")  # a quiet Sunday only
    assert incremental_lookback_days(stored, datetime(2026, 11, 18, 1), today) == 3


def test_lookback_reaches_back_over_a_hole_in_the_stored_calls():
    today = date(2026, 11, 18)
    stored = _calls("2026-11-03", "2026-11-04", "2026-11-15", "2026-11-16", "2026-11-17")
    # 5 Nov is the first missing day: 14 days back from the 18th, inclusive.
    assert incremental_lookback_days(stored, datetime(2026, 11, 18, 1), today) == 14


def test_lookback_reaches_back_to_the_last_complete_sync_and_is_capped():
    today = date(2026, 11, 18)
    assert incremental_lookback_days([], datetime(2026, 11, 10, 1), today) == 9
    assert incremental_lookback_days([], datetime(2026, 1, 1), today) == 30
    assert incremental_lookback_days([], None, today) == 3


# ── Meta: nested edges ask for more than the default 25 rows ────────────────

def test_preset_fields_ask_for_every_ad_in_a_campaign():
    fields = _campaigns_fields_for_preset("last_30d")
    assert "adsets.limit(200){" in fields and "ads.limit(200){" in fields
    assert ",ads{" not in fields and ",adsets{" not in fields


# ── fired alerts name the client of a one-client alert ──────────────────────

async def test_fired_alert_names_its_single_target(mock_db):
    now = datetime.utcnow()
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": ALICE, "name": "Acme"})
    await mock_db["alerts"].insert_many([
        {"id": "a1", "user_id": ALICE, "name": "One client", "target_group_ids": ["g1"],
         "condition": {"metric": "cpl", "operator": "lt", "value": 3.5}},
        {"id": "a2", "user_id": ALICE, "name": "Everyone", "target_group_ids": [],
         "condition": {"metric": "cpl", "operator": "lt", "value": 3.5}},
    ])
    await mock_db["alert_notifications"].insert_many([
        {"alert_id": "a1", "user_id": ALICE, "triggered_at": now - timedelta(hours=1), "message": "m", "current_value": 2},
        {"alert_id": "a2", "user_id": ALICE, "triggered_at": now - timedelta(hours=2), "message": "m", "current_value": 2},
    ])
    fired = {f["alert_id"]: f for f in await alert_history.fired_alerts(mock_db, ALICE, 7)}
    assert (fired["a1"]["group_id"], fired["a1"]["client_name"]) == ("g1", "Acme")
    assert (fired["a2"]["group_id"], fired["a2"]["client_name"]) == (None, None)


async def test_alert_label_has_no_dollar_sign(mock_db):
    await mock_db["alerts"].insert_one({"id": "a1", "user_id": ALICE, "name": "CPL", "status": "active",
                                        "condition": {"metric": "cpl", "operator": "lt", "value": 3.5}})
    out = await alert_tools.get_alerts(mock_db, ALICE)
    assert out["alerts"][0]["metric_label"] == "Cost Per Lead"


# ── refresh status: an error a later refresh got past is not reported ───────

async def test_refresh_status_drops_a_stale_ghl_error(mock_db, mock_mongo_client, monkeypatch):
    from services import meta_refresh_manager

    async def no_progress(group_id, mongo_client):
        return None

    monkeypatch.setattr(meta_refresh_manager, "get_refresh_progress", no_progress)
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": ALICE, "ghl_refresh_status": "complete", "ghl_refresh_error": "No GHL token"},
        {"id": "g2", "user_id": ALICE, "ghl_refresh_status": "error", "ghl_refresh_error": "No GHL token"},
    ])
    done = await client_refresh.get_refresh_status(mock_mongo_client, "g1", ALICE)
    failed = await client_refresh.get_refresh_status(mock_mongo_client, "g2", ALICE)
    assert done["ghl_refresh_error"] is None
    assert failed["ghl_refresh_error"] == "No GHL token"


# ── HotProspector connection ────────────────────────────────────────────────

def test_hotprospector_is_connected_through_the_ghl_location():
    linked = {"ghl_location_id": "loc1", "call_log_provider": "hotprospector", "hotprospector_group_id": None}
    assert hotprospector_connected(linked) is True
    assert hotprospector_connected({"ghl_location_id": "loc1", "call_log_provider": "ghl"}) is False
    assert hotprospector_connected({"call_log_provider": "hotprospector"}) is False
    assert hotprospector_connected({"hotprospector_group_id": "old"}) is True
    assert "hp_call" in sources_connected([linked])
    assert "hp_call" not in sources_connected([{"ghl_location_id": "loc1", "call_log_provider": "none"}])


# ── agent readiness ─────────────────────────────────────────────────────────

async def test_a_deleted_client_is_not_a_missing_target(mock_db):
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": ALICE, "name": "Acme", "targets": {"cpl": 20}})
    agent = {"user_id": ALICE, "scope": {"client_group_ids": ["g1", "gone"]}}
    assert await confidence._watched_targets(mock_db, agent) == (True, [])
    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {"targets": {}}})
    assert await confidence._watched_targets(mock_db, agent) == (False, ["Acme"])
    assert await confidence._watched_targets(mock_db, {"user_id": ALICE, "scope": {"client_group_ids": ["gone"]}}) == (False, [])


# ── cut lists say so, and keep the biggest rows ─────────────────────────────

async def test_account_summary_keeps_the_biggest_spenders_and_says_how_many(mock_db):
    await mock_db["client_groups"].insert_many([
        {"id": f"g{i}", "user_id": ALICE, "name": f"C{i}",
         "facebook_cache": {"presets": {"last_7d": {"metrics": {"insights": {"spend": i}}}}}}
        for i in range(25)
    ])
    out = await summary_tools.get_account_summary(mock_db, ALICE)
    assert (out["total_groups"], out["shown_groups"], len(out["groups"])) == (25, 20, 20)
    assert [g["spend"] for g in out["groups"][:3]] == [24, 23, 22]
    assert out["overall"]["spend"] == sum(range(25))


async def test_call_center_stats_say_how_many_are_shown(mock_db):
    await mock_db["client_groups"].insert_many([
        {"id": f"g{i}", "user_id": ALICE, "name": f"C{i}",
         "hotprospector_call_cache": {"last_7d": {"total_calls": i + 1, "answered_calls": 1}}}
        for i in range(22)
    ])
    out = await hp_tools.get_call_center_stats(mock_db, ALICE)
    assert (out["total"], out["shown"], len(out["call_center"])) == (22, 20, 20)
    assert out["call_center"][0]["total_calls"] == 22


def test_cached_insights_say_how_many_are_shown():
    rows = [{"id": str(i), "spend": i, "client_group_id": "g1", "client_group_name": "Acme"} for i in range(30)]
    shaped = shape_cached_insights(rows)
    assert (shaped["total"], shaped["shown"]) == (30, 20)
