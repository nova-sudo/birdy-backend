"""
ai/mcp/alert_mcp.py is the completed reference port every other ai/mcp/*.py
module followed — these tests exercise its business logic (validation,
create/list/update) against an in-memory Mongo, and confirm the hard
security rule: user_id always comes from the verified token, never from an
LLM-supplied argument.
"""

import pytest

from ai.mcp import alert_mcp


@pytest.mark.asyncio
async def test_get_alerts_requires_authentication(mock_db, no_current_user):
    with pytest.raises(ValueError, match="Unauthenticated"):
        await alert_mcp.get_alerts.fn()


@pytest.mark.asyncio
async def test_get_alerts_empty_for_new_user(mock_db, set_current_user):
    set_current_user("alice@example.com")
    result = await alert_mcp.get_alerts.fn()
    assert result == {"alerts": [], "counts": {"total": 0, "active": 0, "triggered": 0, "paused": 0}}


@pytest.mark.asyncio
async def test_create_alert_rejects_invalid_metric(mock_db, set_current_user):
    set_current_user("alice@example.com")
    result = await alert_mcp.create_alert.fn(name="Test", metric="not_a_real_metric", operator="gt", value=100)
    assert "error" in result
    assert "Invalid metric" in result["error"]


@pytest.mark.asyncio
async def test_create_alert_rejects_invalid_operator(mock_db, set_current_user):
    set_current_user("alice@example.com")
    result = await alert_mcp.create_alert.fn(name="Test", metric="spend", operator="not_an_operator", value=100)
    assert "error" in result
    assert "Invalid operator" in result["error"]


@pytest.mark.asyncio
async def test_create_alert_then_list_shows_it_scoped_to_user(mock_db, set_current_user):
    set_current_user("alice@example.com")
    created = await alert_mcp.create_alert.fn(name="High Spend", metric="spend", operator="gt", value=500)
    assert created["success"] is True
    assert created["name"] == "High Spend"

    listed = await alert_mcp.get_alerts.fn()
    assert listed["counts"]["total"] == 1
    assert listed["alerts"][0]["name"] == "High Spend"

    # A different user must not see alice's alert — the whole point of
    # sourcing user_id from the verified token rather than an argument.
    set_current_user("bob@example.com")
    bobs_view = await alert_mcp.get_alerts.fn()
    assert bobs_view["counts"]["total"] == 0


@pytest.mark.asyncio
async def test_update_alert_not_found_returns_error_not_exception(mock_db, set_current_user):
    set_current_user("alice@example.com")
    result = await alert_mcp.update_alert.fn(alert_id="does-not-exist", name="New Name")
    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_update_alert_changes_only_provided_fields(mock_db, set_current_user):
    set_current_user("alice@example.com")
    created = await alert_mcp.create_alert.fn(name="Original", metric="spend", operator="gt", value=100)
    alert_id = created["alert_id"]

    updated = await alert_mcp.update_alert.fn(alert_id=alert_id, name="Renamed")
    assert updated["success"] is True
    assert updated["updated_fields"] == ["name"]

    listed = await alert_mcp.get_alerts.fn()
    assert listed["alerts"][0]["name"] == "Renamed"
    assert listed["alerts"][0]["condition"]["metric"] == "spend"  # untouched
