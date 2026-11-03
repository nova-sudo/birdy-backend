from datetime import datetime, timedelta

import pytest

from core.database import DB_NAME
from services.alert_service import evaluate_alert
from services.creatives import posting
from services.creatives.alerts import create_verdict_alert, evaluate_creative_alert
from services.creatives.store import add_test_ads, create_test, get_test, update_test

U = "alert-user@example.com"


async def seed(db, *, verdict="testing", verdict_at=None, scores=((30.0,), ), post_states=None,
               status="testing", on_verdict=None, uid=U):
    t = await create_test(db, uid, name="Hook", origin="new", creative={"format": "video"},
                          targets={}, on_verdict=on_verdict)
    await update_test(db, uid, t["id"], {"status": status, "verdict": verdict, "verdict_at": verdict_at})
    rows = []
    for i, (cpl,) in enumerate(scores):
        rows.append({"ad_account_id": f"act_{i}", "client_group_id": f"g{i}", "adset_id": f"as{i}",
                     "post_state": (post_states or {}).get(i, "active"),
                     "meta": {"ad_id": f"{uid}-ad{i}"},
                     "score": {"state": "pass", "cpl": cpl, "ctr": 1.5, "hook_rate": 20.0}})
        await db["client_groups"].insert_one({"id": f"g{i}", "user_id": uid, "name": f"Client {i}"})
    await add_test_ads(db, uid, t["id"], rows)
    return await get_test(db, uid, t["id"])


def alert(metric, op="gt", value=0, **kw):
    return {"id": "a1", "user_id": U, "scope": "creative_tests",
            "condition": {"metric": metric, "operator": op, "value": value, "period": "week"},
            "creative_test_ids": [], "tracking_mode": "total", "last_triggered_at": None, **kw}


async def test_client_alert_path_unchanged(mock_mongo_client):
    res = await evaluate_alert({"id": "x", "user_id": U, "condition": {"metric": "spend", "operator": "gt",
                                                                       "value": 5}}, mock_mongo_client)
    assert res["message"] == "No client groups found for this alert"


async def test_scope_routes_to_creative_evaluator(mock_db, mock_mongo_client):
    t = await seed(mock_db, verdict="winner", verdict_at=datetime(2026, 11, 1))
    res = await evaluate_alert(alert("creative_verdict", creative_test_ids=[t["id"]]), mock_mongo_client)
    assert res["triggered"] is True


async def test_verdict_triggers_once(mock_db):
    t = await seed(mock_db, verdict="winner", verdict_at=datetime(2026, 11, 1))
    a = alert("creative_verdict", creative_test_ids=[t["id"]])
    res = await evaluate_creative_alert(a, mock_db)
    assert res["triggered"] and res["current_value"] == 1
    assert res["message"] == f"{t['code']} is a winner"
    a["last_triggered_at"] = datetime(2026, 11, 2)
    again = await evaluate_creative_alert(a, mock_db)
    assert not again["triggered"] and again["current_value"] == 0
    await update_test(mock_db, U, t["id"], {"verdict": "loser", "verdict_at": datetime(2026, 11, 3)})
    assert (await evaluate_creative_alert(a, mock_db))["message"].endswith("is a loser")


async def test_verdict_testing_not_triggered(mock_db):
    t = await seed(mock_db)
    res = await evaluate_creative_alert(alert("creative_verdict", creative_test_ids=[t["id"]]), mock_db)
    assert not res["triggered"]


async def test_other_users_tests_ignored(mock_db):
    await seed(mock_db, verdict="winner", verdict_at=datetime(2026, 11, 1), uid="other")
    assert not (await evaluate_creative_alert(alert("creative_verdict"), mock_db))["triggered"]


async def test_per_account_cpl_lists_only_crossing(mock_db):
    await seed(mock_db, scores=((50.0,), (20.0,), (40.0,)))
    a = alert("creative_cpl", op="gt", value=35, tracking_mode="per_client")
    res = await evaluate_creative_alert(a, mock_db)
    assert res["triggered"]
    crossing = {r["group_id"]: r for r in res["per_client_results"] if r["triggered"]}
    assert set(crossing) == {"act_0", "act_2"}
    assert crossing["act_0"]["group_name"] == "Client 0"
    assert res["message"] == "Triggered for: Client 0, Client 2"
    assert res["threshold"] == 35.0


async def test_total_cpl_lt(mock_db):
    await seed(mock_db, scores=((50.0,), (20.0,)))
    res = await evaluate_creative_alert(alert("creative_cpl", op="lt", value=25), mock_db)
    assert res["triggered"] and res["current_value"] == 20.0


async def test_pct_operator_not_available(mock_db):
    await seed(mock_db)
    res = await evaluate_creative_alert(alert("creative_cpl", op="pct_drop", value=10), mock_db)
    assert res["triggered"] is False
    assert res["message"] == "Not available for creative tests"


async def test_post_failed(mock_db):
    await seed(mock_db, scores=((30.0,), (30.0,)), post_states={1: "failed"})
    res = await evaluate_creative_alert(alert("creative_post_failed", tracking_mode="per_client"), mock_db)
    assert res["triggered"] and res["current_value"] == 1
    assert [r["group_id"] for r in res["per_client_results"] if r["triggered"]] == ["act_1"]
    ok = await seed(mock_db, uid="u3")
    assert not (await evaluate_creative_alert({**alert("creative_post_failed"), "user_id": "u3"}, mock_db))["triggered"]


async def test_selected_ids_limit_scope(mock_db):
    a = await seed(mock_db, scores=((50.0,),))
    await seed(mock_db, scores=((10.0,),))
    res = await evaluate_creative_alert(alert("creative_cpl", op="gt", value=35, creative_test_ids=[a["id"]]),
                                        mock_db)
    assert res["triggered"]
    res = await evaluate_creative_alert(alert("creative_cpl", op="gt", value=35,
                                              creative_test_ids=["nope"]), mock_db)
    assert not res["triggered"]


async def test_create_verdict_alert_maps_channels(mock_db):
    t = await seed(mock_db, on_verdict={"notify_channels": ["in_app", "slack"]})
    alert_id = await create_verdict_alert(mock_db, U, t)
    doc = await mock_db["alerts"].find_one({"id": alert_id})
    assert doc["scope"] == "creative_tests" and doc["creative_test_ids"] == [t["id"]]
    assert doc["notification_channels"] == ["in_app", "slack"]
    assert doc["condition"]["metric"] == "creative_verdict" and doc["user_id"] == U
    assert doc["status"] == "active" and doc["last_triggered_at"] is None


async def test_go_live_creates_alert_once(mock_db, mock_mongo_client, monkeypatch):
    t = await seed(mock_db, status="posting", post_states={0: "created"},
                   on_verdict={"notify_channels": ["in_app"]})

    async def set_status(*a, **k):
        return None
    monkeypatch.setattr(posting, "set_object_status", set_status)
    await posting.go_live(mock_db, mock_mongo_client, U, t["id"])
    fresh = await get_test(mock_db, U, t["id"])
    aid = fresh["on_verdict"]["alert_id"]
    assert await mock_db["alerts"].count_documents({"id": aid}) == 1
    await posting._ensure_verdict_alert(mock_db, U, fresh)  # idempotent
    assert await mock_db["alerts"].count_documents({"scope": "creative_tests"}) == 1


async def test_go_live_without_channels_creates_nothing(mock_db, mock_mongo_client, monkeypatch):
    t = await seed(mock_db, status="posting", post_states={0: "created"}, on_verdict={"notify_channels": []})

    async def set_status(*a, **k):
        return None
    monkeypatch.setattr(posting, "set_object_status", set_status)
    await posting.go_live(mock_db, mock_mongo_client, U, t["id"])
    assert await mock_db["alerts"].count_documents({}) == 0


async def test_archive_removes_auto_alert(mock_db, mock_mongo_client, monkeypatch):
    from routers import creatives
    t = await seed(mock_db, on_verdict={"notify_channels": ["in_app"]})
    aid = await create_verdict_alert(mock_db, U, t)
    await update_test(mock_db, U, t["id"], {"on_verdict": {**t["on_verdict"], "alert_id": aid}})
    await mock_db["alerts"].insert_one({"id": "keep", "user_id": U})

    class Ctx:
        async def __aenter__(self): return mock_mongo_client
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(creatives, "get_mongo_client", lambda: Ctx())
    await creatives.archive_creative(t["id"], current_user=U)
    assert await mock_db["alerts"].find_one({"id": aid}) is None
    assert await mock_db["alerts"].find_one({"id": "keep"}) is not None
