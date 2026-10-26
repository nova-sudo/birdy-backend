import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
import routers.dashboard as dashboard_router
from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.store import create_agent, list_rules
from dependencies import get_current_user
from main import app

U = "u1@example.com"
OTHER = "u2@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(dashboard_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def group(db, gid="g1", user=U, targets=None):
    doc = {"id": gid, "user_id": user, "name": gid}
    if targets is not None:
        doc["targets"] = targets
    await db["client_groups"].insert_one(doc)


def make_agent(api):
    r = api.post("/api/agents")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_get(api):
    a = make_agent(api)
    assert a["id"] and "_id" not in a
    assert a["status"] == "draft" and a["permissions"]["budgets"] is False
    for k in ("last_run", "last_run_at", "scope", "guardrails", "client_caps", "training", "confidence"):
        assert k in a
    assert api.post("/api/agents").status_code == 409
    assert [x["id"] for x in api.get("/api/agents").json()] == [a["id"]]
    assert api.get(f"/api/agents/{a['id']}").json()["id"] == a["id"]


def test_other_users_agent_404(api):
    a = make_agent(api)
    api.state["user"] = OTHER
    assert api.get("/api/agents").json() == []
    for method, path in [
        ("get", f"/api/agents/{a['id']}"),
        ("patch", f"/api/agents/{a['id']}"),
        ("get", f"/api/agents/{a['id']}/rules"),
        ("get", f"/api/agents/{a['id']}/work"),
        ("get", f"/api/agents/{a['id']}/memory"),
        ("delete", f"/api/agents/{a['id']}/memory/m1"),
        ("put", f"/api/agents/{a['id']}/client-targets/g1"),
    ]:
        kw = {"json": {}} if method in ("patch", "put") else {}
        assert getattr(api, method)(path, **kw).status_code == 404, path


async def test_patch_scope_and_foreign_group(api, mock_db):
    await group(mock_db, "g1")
    await group(mock_db, "gx", user=OTHER)
    a = make_agent(api)
    r = api.patch(f"/api/agents/{a['id']}", json={"scope": {"client_group_ids": ["g1"]}})
    assert r.status_code == 200
    assert r.json()["scope"]["client_group_ids"] == ["g1"]
    assert r.json()["scope"]["schedule"]["time"] == "07:00"


async def test_patch_rejects_foreign_client_group(api, mock_db):
    await group(mock_db, "gx", user=OTHER)
    a = make_agent(api)
    r = api.patch(f"/api/agents/{a['id']}", json={"scope": {"client_group_ids": ["gx"]}})
    assert r.status_code == 400


def test_patch_rejects_status_training_unknown(api):
    a = make_agent(api)
    for body in ({"status": "active"}, {"training": {}}, {"bogus": 1}):
        assert api.patch(f"/api/agents/{a['id']}", json=body).status_code == 422


async def test_budgets_forced_false(api, mock_db):
    await group(mock_db, "g1")
    a = make_agent(api)
    r = api.patch(f"/api/agents/{a['id']}", json={"permissions": {"budgets": True, "campaigns": True},
                                                 "guardrails": {"max_pauses_per_client_day": 5},
                                                 "client_caps": {"g1": 2}})
    assert r.status_code == 200
    p = r.json()["permissions"]
    assert p["budgets"] is False and p["campaigns"] is True
    assert r.json()["guardrails"]["max_pauses_per_client_day"] == 5
    assert r.json()["guardrails"]["protect_last_live_ad"] is True
    assert r.json()["client_caps"] == {"g1": 2}


async def test_rules_list_and_patch(api, mock_db):
    await group(mock_db, "g1")
    a = make_agent(api)
    rules = api.get(f"/api/agents/{a['id']}/rules").json()
    assert len(rules) == 4 and all("id" in r and "_id" not in r for r in rules)
    pause = next(r for r in rules if r["kind"] == "pause_over_target")
    r = api.patch(f"/api/agents/{a['id']}/rules/{pause['id']}", json={
        "params": {"cpl_multiple": 2}, "client_overrides": {"g1": {"patience_spend": 30}},
        "mode": "flag", "enabled": False})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["params"]["cpl_multiple"] == 2 and b["params"]["patience_days"] == 3
    assert b["client_overrides"] == {"g1": {"patience_spend": 30}}
    assert b["mode"] == "flag" and b["enabled"] is False and b["version"] == 2
    assert api.patch(f"/api/agents/{a['id']}/rules/nope", json={"mode": "ask"}).status_code == 404


async def test_rule_patch_rejects_unknown_param(api, mock_db):
    await group(mock_db, "g1")
    a = make_agent(api)
    rid = api.get(f"/api/agents/{a['id']}/rules").json()[0]["id"]
    url = f"/api/agents/{a['id']}/rules/{rid}"
    assert api.patch(url, json={"params": {"nonsense": 1}}).status_code == 400
    kind = api.get(f"/api/agents/{a['id']}/rules").json()[0]["kind"]
    key = next(iter(DEFAULT_PARAMS[kind]))
    assert api.patch(url, json={"params": {key: -1}}).status_code == 400
    assert api.patch(url, json={"params": {key: "5"}}).status_code == 400
    assert api.patch(url, json={"client_overrides": {"g1": {"nonsense": 1}}}).status_code == 400
    assert api.patch(url, json={"client_overrides": {"g1": {key: -2}}}).status_code == 400
    await group(mock_db, "gx", user=OTHER)
    assert api.patch(url, json={"client_overrides": {"gx": {key: 1}}}).status_code == 400


def test_rule_patch_rejects_auto_mode(api):
    a = make_agent(api)
    rid = api.get(f"/api/agents/{a['id']}/rules").json()[0]["id"]
    url = f"/api/agents/{a['id']}/rules/{rid}"
    assert api.patch(url, json={"mode": "auto"}).status_code == 400
    assert api.patch(url, json={"mode": "weird"}).status_code == 400
    assert api.patch(url, json={"bogus": 1}).status_code == 422


async def test_client_target_merges(api, mock_db):
    await group(mock_db, "g1", targets={"cpa": 40, "monthly_wins": 12})
    await group(mock_db, "g2")
    a = make_agent(api)
    api.patch(f"/api/agents/{a['id']}", json={"scope": {"client_group_ids": ["g1"]}})
    r = api.put(f"/api/agents/{a['id']}/client-targets/g1", json={"cpl": 9.5})
    assert r.status_code == 200
    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["cpl"] == 9.5 and stored["cpa"] == 40 and stored["monthly_wins"] == 12
    assert r.json()["targets"]["cpl"] == 9.5
    # An owned client outside the scope can still get a target (targets belong to
    # the client), and the scope is left alone. Unknown or foreign group -> 404.
    r2 = api.put(f"/api/agents/{a['id']}/client-targets/g2", json={"cpl": 1})
    assert r2.status_code == 200 and r2.json()["targets"]["cpl"] == 1
    assert api.get(f"/api/agents/{a['id']}").json()["scope"]["client_group_ids"] == ["g1"]
    assert api.put(f"/api/agents/{a['id']}/client-targets/zzz", json={"cpl": 1}).status_code == 404
    await group(mock_db, "gx", user=OTHER)
    assert api.put(f"/api/agents/{a['id']}/client-targets/gx", json={"cpl": 1}).status_code == 404
    # clearing
    api.put(f"/api/agents/{a['id']}/client-targets/g1", json={"cpl": None})
    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert "cpl" not in stored and stored["cpa"] == 40


async def test_work_open_and_auto_applied(api, mock_db):
    a = make_agent(api)
    now = datetime.utcnow()

    def sug(i, **kw):
        d = {"_id": i, "id": i, "user_id": U, "agent_id": a["id"], "status": "open",
             "created_at": now, "agent": "media_buying:pause_over_target",
             "rule_mode": "ask", "title": i}
        d.update(kw)
        return d

    await mock_db["ai_suggestions"].insert_many([
        sug("old", created_at=now - timedelta(hours=5)),
        sug("new"),
        sug("other_agent", agent_id="zzz"),
        sug("auto", status="applied", applied_at=now - timedelta(hours=2), decided_by={"actor": "birdy"}),
        sug("auto_old", status="applied", applied_at=now - timedelta(hours=30), decided_by={"actor": "birdy"}),
        sug("by_user", status="applied", applied_at=now, decided_by={"actor": "user"}),
    ])
    r = api.get(f"/api/agents/{a['id']}/work")
    assert r.status_code == 200
    j = r.json()
    assert [x["id"] for x in j["open"]] == ["new", "old"]
    assert [x["id"] for x in j["auto_applied"]] == ["auto"]
    assert j["open"][0]["rule_kind"] == "pause_over_target"
    assert j["open"][0]["rule_mode"] == "ask"


async def test_memory_list_and_delete(api, mock_db):
    from ai.agents.store import add_memory
    a = make_agent(api)
    m = await add_memory(mock_db, a["id"], text="x", kind="fact", source="note")
    r = api.get(f"/api/agents/{a['id']}/memory").json()
    assert [x["id"] for x in r] == [m["_id"]]
    assert api.delete(f"/api/agents/{a['id']}/memory/{m['_id']}").status_code == 200
    assert api.get(f"/api/agents/{a['id']}/memory").json() == []
    assert api.delete(f"/api/agents/{a['id']}/memory/{m['_id']}").status_code == 404


async def _suggestion(db, **kw):
    d = {"_id": "s1", "id": "s1", "user_id": U, "status": "open", "title": "Pause X",
         "client_name": "Aura", "client_group_id": "g1", "agent_id": "a1", "rule_id": "r1",
         "agent": "media_buying:pause_over_target", "created_at": datetime.utcnow()}
    d.update(kw)
    await db["ai_suggestions"].insert_one(d)


async def test_deny_records_reason_and_note(api, mock_db):
    await _suggestion(mock_db)
    r = api.post("/api/dashboard/suggestions/s1/deny", json={"reason_code": "creative_stale", "note": "refresh soon"})
    # No action on this item, so nothing is learned (memory is None).
    assert r.status_code == 200 and r.json() == {"ok": True, "memory": None}
    assert (await mock_db["ai_suggestions"].find_one({"_id": "s1"}))["status"] == "dismissed"
    act = await mock_db["ai_activity"].find_one({"ref_id": "s1"})
    assert act["reason_code"] == "creative_stale" and act["note"] == "refresh soon"
    assert act["agent_id"] == "a1" and act["rule_id"] == "r1"


async def test_deny_rejects_unknown_reason(api, mock_db):
    await _suggestion(mock_db)
    r = api.post("/api/dashboard/suggestions/s1/deny", json={"reason_code": "because"})
    assert r.status_code == 400
    assert (await mock_db["ai_suggestions"].find_one({"_id": "s1"}))["status"] == "open"


async def test_deny_other_users_suggestion_404(api, mock_db):
    await _suggestion(mock_db, user_id=OTHER)
    assert api.post("/api/dashboard/suggestions/s1/deny", json={"reason_code": "other"}).status_code == 404


async def test_deny_busy_409(api, mock_db):
    await _suggestion(mock_db, applying_at=datetime.utcnow(), applying_token="t")
    assert api.post("/api/dashboard/suggestions/s1/deny", json={"reason_code": "other"}).status_code == 409


async def test_suggestion_to_api_new_fields():
    out = dashboard_router._suggestion_to_api({
        "id": "s", "agent": "media_buying:revive_winner", "agent_id": "a", "rule_mode": "flag",
        "blocked_reason": {"code": "c", "message": "m"}, "auto_failed": True, "undone_at": None})
    assert out["rule_kind"] == "revive_winner" and out["agent_id"] == "a" and out["rule_mode"] == "flag"
    assert out["blocked_reason"]["code"] == "c" and out["auto_failed"] is True and out["undone_at"] is None
    assert dashboard_router._suggestion_to_api({"id": "x", "agent": "useless_ad_purger"})["rule_kind"] is None
    assert out["has_action"] is False
    assert dashboard_router._suggestion_to_api({"id": "y", "action": {"type": "pause_ads"}})["has_action"] is True


def test_schedule_validation_and_partial_merge(api):
    a = make_agent(api)
    url = f"/api/agents/{a['id']}"
    assert api.patch(url, json={"scope": {"schedule": {"tz": "Nope/Zone"}}}).status_code == 400
    assert api.patch(url, json={"scope": {"schedule": {"time": "7am"}}}).status_code == 400
    assert api.patch(url, json={"scope": {"schedule": {"time": "24:00"}}}).status_code == 400
    assert api.patch(url, json={"scope": {"schedule": "x"}}).status_code == 400
    assert api.patch(url, json={"scope": {"bogus": 1}}).status_code == 400
    r = api.patch(url, json={"scope": {"schedule": {"tz": "America/New_York"}}})
    assert r.status_code == 200
    assert r.json()["scope"]["schedule"] == {"time": "07:00", "tz": "America/New_York"}
    r = api.patch(url, json={"scope": {"schedule": {"time": "09:30"}}})
    assert r.json()["scope"]["schedule"] == {"time": "09:30", "tz": "America/New_York"}


async def test_client_caps_validated_and_negative_cpl_rejected(api, mock_db):
    await group(mock_db, "g1")
    a = make_agent(api)
    url = f"/api/agents/{a['id']}"
    assert api.patch(url, json={"client_caps": {"zzz": 2}}).status_code == 400
    assert api.patch(url, json={"client_caps": {"g1": -1}}).status_code == 400
    api.patch(url, json={"scope": {"client_group_ids": ["g1"]}})
    assert api.put(f"{url}/client-targets/g1", json={"cpl": -3}).status_code == 422
