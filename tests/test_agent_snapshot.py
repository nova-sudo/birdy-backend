import json
from datetime import date, datetime
from pathlib import Path

from ai.agents.snapshot import (
    AdDay, AdState, AdsetState, ClientCtx,
    leads_from_insight, load_ad_days, load_client_ctx, load_state,
)
from agent_history_helpers import seed_ad_days

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ad_insight_row.json").read_text())
GID = "cg1"


def _row(ad_id, day, spend, leads, updated=datetime(2026, 10, 21)):
    r = json.loads(json.dumps(FIXTURE))
    r.pop("_comment", None)
    r.update(user_id="u", client_group_id=GID, ad_id=ad_id, date_start=day, updated_at=updated)
    r["insight_data"].update(ad_id=ad_id, spend=str(spend), date_start=day)
    r["insight_data"]["actions"] = [{"action_type": "lead", "value": str(leads)}]
    return r


def test_leads_from_insight_reads_lead_results():
    assert leads_from_insight(FIXTURE["insight_data"]) == 3
    assert leads_from_insight({"spend": "1"}) == 0
    assert leads_from_insight(None) == 0


async def test_load_ad_days_sorts_and_dedups(mock_db):
    await seed_ad_days(mock_db, [
        _row("a1", "2026-10-02", 10, 1),
        _row("a1", "2026-10-01", 5, 0),
        _row("a1", "2026-10-02", 99, 9, updated=datetime(2026, 10, 25)),  # newer dup wins
        _row("a2", "2026-10-01", 7, 2),
        _row("a1", "2026-09-01", 1, 1),  # out of range
    ])
    out = await load_ad_days(mock_db, "u", GID, date(2026, 10, 1), date(2026, 10, 31))
    assert set(out) == {"a1", "a2"}
    assert [d.day for d in out["a1"]] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert out["a1"][1] == AdDay("a1", "5000000000001", "4000000000001", "Fixture Ad A",
                                 date(2026, 10, 2), 99.0, 9, "Fixture Campaign")
    assert out["a1"][0].campaign_name == "Fixture Campaign"


async def test_load_client_ctx_target_and_overrides(mock_db):
    await mock_db.client_groups.insert_one({
        "id": GID, "user_id": "u", "name": "Acme", "ad_account_currency": "GBP",
        "targets": {"cpl": 25},
    })
    await mock_db.agent_rules.insert_many([
        {"_id": "r1", "agent_id": "ag", "kind": "pause_over_target",
         "client_overrides": {GID: {"patience_spend": 100}, "other": {"x": 1}}},
        {"_id": "r2", "agent_id": "ag", "kind": "revive_old_winners", "client_overrides": {}},
        {"_id": "r3", "agent_id": "ag", "kind": "flag_no_spend",
         "client_overrides": {GID: {"min_hours": 48}}},
        {"_id": "r4", "agent_id": "someone-else", "kind": "flag_no_spend",
         "client_overrides": {GID: {"nope": 1}}},
    ])
    agent = {"_id": "ag", "scope": {"schedule": {"tz": "Europe/London"}}}
    ctx = await load_client_ctx(mock_db, "u", GID, agent)
    assert ctx == ClientCtx(GID, "Acme", "£", 25.0,
                            {"pause_over_target": {"patience_spend": 100},
                             "flag_no_spend": {"min_hours": 48}},
                            "Europe/London")


async def test_load_client_ctx_no_target(mock_db):
    await mock_db.client_groups.insert_one({"id": GID, "user_id": "u", "name": "Acme"})
    ctx = await load_client_ctx(mock_db, "u", GID, {"_id": "ag", "scope": {"schedule": {"tz": "UTC"}}})
    assert ctx.target_cpl is None and ctx.currency_symbol == "$" and ctx.tz == "UTC"


async def test_load_state(mock_db):
    await mock_db.meta_ad_state.insert_many([
        {"client_group_id": GID, "ad_id": "a1", "adset_id": "s1", "campaign_id": "c1", "name": "Ad",
         "effective_status": "PAUSED", "created_time": datetime(2026, 9, 1), "paused_at": "2026-10-10"},
        {"client_group_id": "other", "ad_id": "zz", "name": "x", "effective_status": "ACTIVE"},
    ])
    await mock_db.meta_adset_state.insert_one(
        {"client_group_id": GID, "adset_id": "s1", "campaign_id": "c1", "name": "S",
         "effective_status": "ACTIVE", "campaign_status": "ACTIVE", "daily_budget": 20.0, "lifetime_budget": 0.0})
    ads, adsets = await load_state(mock_db, GID)
    assert ads == {"a1": AdState("a1", "s1", "c1", "Ad", "PAUSED", datetime(2026, 9, 1), date(2026, 10, 10))}
    assert adsets == {"s1": AdsetState("s1", "c1", "S", "ACTIVE", "ACTIVE", 20.0, 0.0)}
