"""get_fired_alerts / explain_alert (watch_mcp) and notification_channels on create_alert/update_alert."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import alert_mcp, watch_mcp
from services import alert_service

pytestmark = pytest.mark.asyncio

NOW = datetime.utcnow()


async def _alert(db, user, aid="a1", name="High spend", **kw):
    doc = {"id": aid, "user_id": user, "name": name, "status": "active",
           "condition": {"metric": "spend", "operator": "gt", "value": 100.0, "period": "day"},
           "target_group_ids": [], "notification_channels": ["in_app"], **kw}
    await db["alerts"].insert_one(doc)
    return doc


async def _note(db, user, aid, ago, group_id=None, msg="fired"):
    row = {"alert_id": aid, "user_id": user, "message": msg, "current_value": 150.0,
           "triggered_at": NOW - ago, "read": False}
    if group_id:
        row["group_id"] = group_id
    await db["alert_notifications"].insert_one(row)


async def test_fired_alerts_window_and_order(mock_db, set_current_user):
    set_current_user("alice@example.com")
    await _alert(mock_db, "alice@example.com", "a1", "Alpha")
    await _alert(mock_db, "alice@example.com", "a2", "Beta")
    await _note(mock_db, "alice@example.com", "a1", timedelta(days=2))
    await _note(mock_db, "alice@example.com", "a2", timedelta(days=1))
    await _note(mock_db, "alice@example.com", "a1", timedelta(days=30))  # outside window
    out = await watch_mcp.get_fired_alerts.fn(days=7)
    assert [f["alert_name"] for f in out["fired"]] == ["Beta", "Alpha"]
    assert out["fired"][0]["condition_display"]
    assert out["days"] == 7


async def test_fired_alerts_collapse_repeated_rows(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)
    for m in (240, 180, 120, 60):  # hourly passes while it stays triggered
        await _note(mock_db, u, "a1", timedelta(minutes=m))
    await _note(mock_db, u, "a1", timedelta(days=3))  # a separate, earlier firing
    out = await watch_mcp.get_fired_alerts.fn(days=7)
    assert len(out["fired"]) == 2
    latest = out["fired"][0]
    assert latest["count"] == 4
    assert latest["first_triggered_at"] < latest["last_seen_at"]
    assert out["fired"][1]["count"] == 1


async def test_fired_alerts_gap_is_longer_than_the_hourly_cadence(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)
    for m in (120, 60):  # 60 minutes apart: one firing
        await _note(mock_db, u, "a1", timedelta(minutes=m))
    out = await watch_mcp.get_fired_alerts.fn(days=7)
    assert len(out["fired"]) == 1 and out["fired"][0]["count"] == 2


async def test_fired_alerts_rows_two_hours_apart_are_two_firings(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)
    for m in (240, 120):  # 120 minutes apart: two firings
        await _note(mock_db, u, "a1", timedelta(minutes=m))
    out = await watch_mcp.get_fired_alerts.fn(days=7)
    assert len(out["fired"]) == 2 and [f["count"] for f in out["fired"]] == [1, 1]


@pytest.mark.parametrize("gap,firings", [(90, 1), (91, 2)])
async def test_fired_alerts_gap_boundary(mock_db, set_current_user, gap, firings):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)
    for m in (300, 300 - gap):
        await _note(mock_db, u, "a1", timedelta(minutes=m))
    out = await watch_mcp.get_fired_alerts.fn(days=7)
    assert len(out["fired"]) == firings


async def test_fired_alerts_group_name_by_id(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": u, "name": "Acme Dental"})
    await _alert(mock_db, u)
    await _note(mock_db, u, "a1", timedelta(hours=1), group_id="g1")
    await _note(mock_db, u, "a1", timedelta(hours=1))
    out = await watch_mcp.get_fired_alerts.fn()
    names = {f["group_id"]: f["client_name"] for f in out["fired"]}
    assert names == {"g1": "Acme Dental", None: None}


async def test_fired_alerts_other_user_sees_none(mock_db, set_current_user):
    await _alert(mock_db, "alice@example.com")
    await _note(mock_db, "alice@example.com", "a1", timedelta(hours=1))
    set_current_user("bob@example.com")
    assert (await watch_mcp.get_fired_alerts.fn())["fired"] == []


@pytest.mark.parametrize("days", [0, 91, True, "7"])
async def test_fired_alerts_days_validated(mock_db, set_current_user, days):
    set_current_user("alice@example.com")
    with pytest.raises(ToolError):
        await watch_mcp.get_fired_alerts.fn(days=days)


@pytest.fixture
def fake_eval(monkeypatch):
    calls = []

    async def ev(alert, client):
        calls.append(alert["id"])
        return {"triggered": True, "message": "Spend 150 > 100", "current_value": 150.0}
    monkeypatch.setattr(alert_service, "evaluate_alert", ev)
    return calls


async def test_explain_alert_returns_everything(mock_db, set_current_user, fake_eval):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u, last_eval_result={"per_client_results": [{"group_id": "g1"}], "message": "old"})
    for i in range(12):
        await _note(mock_db, u, "a1", timedelta(days=i + 1))
    out = await watch_mcp.explain_alert.fn("a1")
    assert out["alert"]["name"] == "High spend"
    assert out["condition_display"]
    assert out["last_eval_result"]["per_client_results"][0]["group_id"] == "g1"
    assert len(out["recent_notifications"]) == 10
    assert out["now"]["triggered"] is True


async def test_explain_alert_other_user_not_found(mock_db, set_current_user, fake_eval):
    await _alert(mock_db, "alice@example.com")
    set_current_user("bob@example.com")
    with pytest.raises(ToolError, match="not found"):
        await watch_mcp.explain_alert.fn("a1")
    assert fake_eval == []


async def test_explain_alert_timeout_still_returns_rest(mock_db, set_current_user, monkeypatch):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)

    async def slow(alert, client):
        await asyncio.sleep(10)
    monkeypatch.setattr(alert_service, "evaluate_alert", slow)
    monkeypatch.setattr(watch_mcp, "EVAL_TIMEOUT_SECONDS", 0.05)
    out = await watch_mcp.explain_alert.fn("a1")
    assert "error" in out["now"] and "timed out" in out["now"]["error"]
    assert out["alert"]["id"] == "a1"


async def test_explain_alert_eval_error_still_returns_rest(mock_db, set_current_user, monkeypatch):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u)

    async def boom(alert, client):
        raise RuntimeError("meta down")
    monkeypatch.setattr(alert_service, "evaluate_alert", boom)
    out = await watch_mcp.explain_alert.fn("a1")
    assert "error" in out["now"]
    assert out["condition_display"]


async def test_explain_alert_writes_nothing(mock_db, set_current_user, fake_eval):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u, last_eval_result={"message": "old"})
    before_alert = await mock_db["alerts"].find_one({"id": "a1"})
    await watch_mcp.explain_alert.fn("a1")
    assert await mock_db["alerts"].find_one({"id": "a1"}) == before_alert
    assert await mock_db["alert_notifications"].count_documents({}) == 0


# ---- channels on create_alert / update_alert ----

async def test_create_alert_with_email_channel(mock_db, set_current_user):
    set_current_user("alice@example.com")
    r = await alert_mcp.create_alert.fn(name="E", metric="spend", operator="gt", value=1,
                                        notification_channels=["in_app", "email", "email"])
    doc = await mock_db["alerts"].find_one({"id": r["alert_id"]})
    assert doc["notification_channels"] == ["in_app", "email"]
    assert r["notification_channels"] == ["in_app", "email"]


async def test_create_alert_default_channels_unchanged(mock_db, set_current_user):
    set_current_user("alice@example.com")
    r = await alert_mcp.create_alert.fn(name="E", metric="spend", operator="gt", value=1)
    doc = await mock_db["alerts"].find_one({"id": r["alert_id"]})
    assert doc["notification_channels"] == ["in_app"]
    assert "warning" not in r


async def test_create_alert_invalid_channel(mock_db, set_current_user):
    set_current_user("alice@example.com")
    with pytest.raises(ToolError, match="in_app, slack, email"):
        await alert_mcp.create_alert.fn(name="E", metric="spend", operator="gt", value=1,
                                        notification_channels=["sms"])
    assert await mock_db["alerts"].count_documents({}) == 0


async def test_create_alert_slack_not_connected_warns(mock_db, set_current_user):
    set_current_user("alice@example.com")
    r = await alert_mcp.create_alert.fn(name="E", metric="spend", operator="gt", value=1,
                                        notification_channels=["slack"])
    assert r["success"] is True
    assert "Slack isn't connected" in r["warning"]


async def test_create_alert_slack_connected_no_warning(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await mock_db["users"].insert_one({"user_id": u, "integrations": {"slack": {"webhook_url": "https://x"}}})
    r = await alert_mcp.create_alert.fn(name="E", metric="spend", operator="gt", value=1,
                                        notification_channels=["slack"])
    assert "warning" not in r


async def test_update_alert_channels_and_none_leaves_untouched(mock_db, set_current_user):
    u = "alice@example.com"
    set_current_user(u)
    await _alert(mock_db, u, notification_channels=["in_app", "slack"])
    await alert_mcp.update_alert.fn(alert_id="a1", name="Renamed")
    assert (await mock_db["alerts"].find_one({"id": "a1"}))["notification_channels"] == ["in_app", "slack"]
    r = await alert_mcp.update_alert.fn(alert_id="a1", notification_channels=["email"])
    assert "notification_channels" in r["updated_fields"]
    assert (await mock_db["alerts"].find_one({"id": "a1"}))["notification_channels"] == ["email"]


async def test_update_alert_channels_invalid_and_cross_account(mock_db, set_current_user):
    await _alert(mock_db, "alice@example.com", notification_channels=["in_app"])
    set_current_user("bob@example.com")
    r = await alert_mcp.update_alert.fn(alert_id="a1", notification_channels=["email"])
    assert "not found" in r["error"].lower()
    assert (await mock_db["alerts"].find_one({"id": "a1"}))["notification_channels"] == ["in_app"]
    set_current_user("alice@example.com")
    with pytest.raises(ToolError, match="in_app, slack, email"):
        await alert_mcp.update_alert.fn(alert_id="a1", notification_channels=["fax"])
