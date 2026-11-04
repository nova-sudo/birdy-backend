import contextlib

import pytest
from fastapi.testclient import TestClient

import routers.agent_stock as stock_router
import routers.agents as agents_router
from ai.agents import claims, store
from ai.agents.stock.settings import (
    DEFAULT_SETTINGS, Zones, resolve_lost_spend, resolve_mode, resolve_zones, settings_errors)
from dependencies import get_current_user
from main import app

GBP_ZONES = {"currency": "GBP", "green_under": 8, "red_over": 10}


def test_default_zones_only_for_matching_currency():
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES}
    assert resolve_zones(s, "g", "GBP") == Zones(8, 10)
    assert resolve_zones(s, "g", "USD") is None


def test_defaults_never_preset_money_limits():
    assert resolve_zones(DEFAULT_SETTINGS, "g", "GBP") is None
    assert DEFAULT_SETTINGS["clients"] == {}


def test_client_override_wins():
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES,
         "clients": {"g": {"zones": {"green_under": 6, "red_over": 8}}}}
    assert resolve_zones(s, "g", "USD") == Zones(6, 8)


def test_red_below_green_is_an_error():
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES,
         "clients": {"g": {"zones": {"green_under": 6, "red_over": 5}}}}
    errs = settings_errors(s, [{"id": "g", "name": "Glow", "ad_account_currency": "GBP"}])
    assert [(e["field"], e["client_group_id"]) for e in errs] == [("zones", "g")]
    bad_default = {**DEFAULT_SETTINGS, "zones": {"currency": "GBP", "green_under": 8, "red_over": 3}}
    assert settings_errors(bad_default, [])[0]["field"] == "zones"


def test_usd_client_without_override_is_an_error_naming_it():
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES}
    errs = settings_errors(s, [{"id": "g1", "name": "Glow", "ad_account_currency": "GBP"},
                               {"id": "g2", "name": "Sunny", "ad_account_currency": "USD"}])
    assert len(errs) == 1 and errs[0]["client_group_id"] == "g2" and "Sunny" in errs[0]["message"]


def test_mode_defaults_to_ask_and_client_override_wins(monkeypatch):
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")
    assert resolve_mode(DEFAULT_SETTINGS, "g", "add") == "ask"
    s = {**DEFAULT_SETTINGS, "clients": {"g": {"modes": {"add": "flag"}}}}
    assert resolve_mode(s, "g", "add") == "flag"


def test_add_is_flag_while_writes_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    assert resolve_mode({**DEFAULT_SETTINGS, "modes": {"add": "auto"}}, "g", "add") == "flag"
    assert resolve_mode(DEFAULT_SETTINGS, "g", "add") == "flag"  # Ask me as well: nothing could post it
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")
    assert resolve_mode({**DEFAULT_SETTINGS, "modes": {"add": "auto"}}, "g", "add") == "auto"
    assert resolve_mode({**DEFAULT_SETTINGS, "modes": {"remove": "auto"}}, "g", "remove") == "auto"


def test_lost_spend_is_six_times_green():
    assert resolve_lost_spend(DEFAULT_SETTINGS, Zones(2.5, 3)) == 15


def test_spec_defaults():
    d = DEFAULT_SETTINGS
    assert d["batches"] == [{"id": "a", "name": "Batch A", "weekdays": [0, 3]},
                            {"id": "b", "name": "Batch B", "weekdays": [1, 4]}]
    assert d["creative"] == {"max_ads_live": 4, "winner_days": 14, "winner_min_leads": 8,
                             "revive_after_days": 60, "major_downtrend_pct": 30, "auto_max_adds": 2,
                             "data_window_days": 7}
    assert (d["alerts"], d["scoring"]["rule_after"]) == ({"prepaid_days": 4, "spend_drop_ratio": 0.5}, 3)
    assert d["modes"] == {"remove": "ask", "add": "ask", "revive": "ask"}


async def test_stock_agent_is_created_with_default_settings(mock_db):
    a = await store.create_agent(mock_db, "u", type=store.TYPE_STOCK)
    assert a["settings"] == DEFAULT_SETTINGS and a["settings"] is not DEFAULT_SETTINGS
    c = await store.create_agent(mock_db, "u", type="custom", name="x")
    assert "settings" not in c


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

U = "u1@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(stock_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: U
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def _group(db, gid, currency="GBP", targets=None):
    doc = {"id": gid, "user_id": U, "name": gid, "ad_account_currency": currency}
    if targets:
        doc["targets"] = targets
    await db["client_groups"].insert_one(doc)


async def _stock(db):
    return (await store.create_agent(db, U, type=store.TYPE_STOCK))["_id"]


async def test_patch_rejects_invalid_mode(api, mock_db):
    aid = await _stock(mock_db)
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {"modes": {"add": "yolo"}}})
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "modes.add"
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {"bogus": 1}})
    assert r.status_code == 400


async def test_patch_merges_and_leaves_the_rest(api, mock_db):
    aid = await _stock(mock_db)
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {
        "creative": {"max_ads_live": 3}, "modes": {"add": "flag"}}})
    s = r.json()["settings"]
    assert r.status_code == 200
    assert s["creative"]["max_ads_live"] == 3 and s["creative"]["winner_days"] == 14
    assert s["modes"] == {"remove": "ask", "add": "flag", "revive": "ask"}


async def test_custom_agent_has_no_stock_settings(api, mock_db):
    c = await store.create_agent(mock_db, U, type="custom", name="x")
    assert api.patch(f"/api/agents/{c['_id']}/settings", json={"settings": {}}).status_code == 400


async def test_patch_client_list_updates_scope_and_prefills_green(api, mock_db):
    aid = await _stock(mock_db)
    await _group(mock_db, "g1", targets={"cpl": 2.5})
    await _group(mock_db, "g2")
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g2"]})
    assert r.status_code == 200
    assert r.json()["scope"]["client_group_ids"] == ["g1", "g2"]
    clients = r.json()["settings"]["clients"]
    assert clients["g1"]["zones"] == {"green_under": 2.5} and "g2" not in clients
    assert "prepaid" not in clients["g1"]
    # A green limit the agency already chose is not overwritten on re-add.
    api.patch(f"/api/agents/{aid}/settings", json={
        "settings": {"clients": {"g1": {"zones": {"green_under": 4}}}}, "client_group_ids": ["g2"]})
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g2"]})
    assert r.json()["settings"]["clients"]["g1"]["zones"]["green_under"] == 4


async def test_unowned_client_is_rejected(api, mock_db):
    aid = await _stock(mock_db)
    assert api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["nope"]}).status_code == 400


async def test_client_list_change_while_active_goes_through_claims(api, mock_db):
    aid = await _stock(mock_db)
    other = await store.create_agent(mock_db, U, type="custom", name="Other")
    for g in ("g1", "g2"):
        await _group(mock_db, g)
    await claims.claim_clients(mock_db, U, other["_id"], ["g2"])
    await mock_db.agents.update_one({"_id": aid}, {"$set": {"status": "active"}})
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g2"]})
    assert r.status_code == 409 and r.json()["conflicts"][0]["client_group_id"] == "g2"
    assert (await mock_db.agents.find_one({"_id": aid}))["scope"]["client_group_ids"] == []
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1"]})
    assert r.status_code == 200 and await claims.holder(mock_db, U, "g1") == aid
    api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": []})
    assert await claims.holder(mock_db, U, "g1") is None


async def test_activate_reports_zone_errors(api, mock_db):
    aid = await _stock(mock_db)
    await _group(mock_db, "g1", currency="USD")
    api.patch(f"/api/agents/{aid}/settings", json={
        "settings": {"zones": GBP_ZONES}, "client_group_ids": ["g1"]})
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and r.json()["errors"][0]["client_group_id"] == "g1"
    assert any("needs its own zones" in m for m in r.json()["missing"])


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zones", [
    {"green_under": 6}, {"red_over": 9}, {"green_under": 6, "red_over": None},
    {"green_under": None, "red_over": 9}])
def test_partial_override_never_falls_back_to_default(zones):
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES, "clients": {"g": {"zones": zones}}}
    assert resolve_zones(s, "g", "GBP") is None
    errs = settings_errors(s, [{"id": "g", "name": "Glow", "ad_account_currency": "GBP"}])
    assert len(errs) == 1 and errs[0]["client_group_id"] == "g" and "Glow" in errs[0]["message"]
    missing = "red" if "green_under" in zones and zones["green_under"] is not None else "green"
    assert missing in errs[0]["message"]


def test_all_null_override_is_no_override():
    s = {**DEFAULT_SETTINGS, "zones": GBP_ZONES,
         "clients": {"g": {"zones": {"green_under": None, "red_over": None}}}}
    assert resolve_zones(s, "g", "GBP") == Zones(8, 10)


def test_resolve_mode_rejects_unknown_kind():
    with pytest.raises(ValueError):
        resolve_mode(DEFAULT_SETTINGS, "g", "alert")


def test_data_window_default():
    assert DEFAULT_SETTINGS["creative"]["data_window_days"] == 7


def test_spec_scoring_and_weekly_defaults():
    assert DEFAULT_SETTINGS["scoring"] == {
        "min_days": 7, "win_min_leads": 5, "lose_cpl_multiple": 1.3,
        "lose_spend_multiple": 6, "lose_max_leads": 1, "rule_after": 3}
    assert DEFAULT_SETTINGS["weekly"] == {"enabled": False, "weekday": 0, "auto_post": False}


@pytest.mark.parametrize("settings", [
    {"creative": {"data_window_days": 0}}, {"creative": {"data_window_days": 1.5}},
    {"creative": {"max_ads_live": 0}}, {"creative": {"winner_days": 0}},
    {"scoring": {"min_days": 0}}, {"alerts": {"spend_drop_ratio": 0}},
    {"alerts": {"spend_drop_ratio": 1.5}}])
async def test_out_of_range_numbers_are_rejected(api, mock_db, settings):
    aid = await _stock(mock_db)
    assert api.patch(f"/api/agents/{aid}/settings", json={"settings": settings}).status_code == 400


async def test_in_range_numbers_are_accepted(api, mock_db):
    aid = await _stock(mock_db)
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {
        "creative": {"data_window_days": 1}, "alerts": {"spend_drop_ratio": 1}}})
    assert r.status_code == 200


async def test_currency_change_nulls_old_amounts(api, mock_db):
    aid = await _stock(mock_db)
    api.patch(f"/api/agents/{aid}/settings", json={"settings": {"zones": GBP_ZONES}})
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {"zones": {"currency": "USD"}}})
    assert r.json()["settings"]["zones"] == {"currency": "USD", "green_under": None, "red_over": None}
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {
        "zones": {"currency": "EUR", "green_under": 3, "red_over": 4}}})
    assert r.json()["settings"]["zones"] == {"currency": "EUR", "green_under": 3, "red_over": 4}
    r = api.patch(f"/api/agents/{aid}/settings", json={"settings": {"zones": {"red_over": 5}}})
    assert r.json()["settings"]["zones"]["green_under"] == 3  # same currency keeps amounts


async def test_no_client_entry_without_something_to_prefill(api, mock_db):
    aid = await _stock(mock_db)
    await _group(mock_db, "g2")
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g2"]})
    assert r.json()["settings"]["clients"] == {}
    assert resolve_zones(r.json()["settings"], "g2", "GBP") is None
    assert resolve_mode(r.json()["settings"], "g2", "revive") == "ask"


async def test_flag_only_is_preserved_across_client_list_patch(api, mock_db):
    aid = await _stock(mock_db)
    for g in ("g1", "g2", "g3"):
        await _group(mock_db, g)
    api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g2"], "only_flag": ["g2"]})
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g2", "g3"]})
    assert r.json()["scope"]["flag_only_client_group_ids"] == ["g2"]
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g1", "g3"]})
    assert r.json()["scope"]["flag_only_client_group_ids"] == []
