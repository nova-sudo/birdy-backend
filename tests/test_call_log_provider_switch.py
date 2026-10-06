"""
Switching a client's call centre after it was created. Onboarding asked once;
a client set to "none" then stayed "none" after HotProspector was connected,
and the Sales Hub had no way to change it.
"""
import pytest
from fastapi import HTTPException

from routers.client_groups import update_call_log_provider

USER = "owner@agency.com"


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _seed(db, *, hp_connected=True):
    user = {"user_id": USER}
    if hp_connected:
        user["integrations"] = {"hotprospector": {"credentials": {"api_uid": "u", "api_key": "k", "connected": True}}}
    await db["users"].insert_one(user)
    await db["client_groups"].insert_many([
        {"id": "g1", "user_id": USER, "call_log_provider": "none", "last_hp_refresh": "old"},
        {"id": "g2", "user_id": USER, "call_log_provider": "none"},
        {"id": "other", "user_id": "someone@else.com", "call_log_provider": "none"},
    ])


async def test_switches_clients_to_hotprospector_and_queues_a_refresh(mock_mongo_client, mock_db):
    await _seed(mock_db)

    out = await update_call_log_provider(_Req({"group_ids": ["g1", "g2"], "provider": "hotprospector"}), current_user=USER)

    assert out == {"provider": "hotprospector", "updated": 2}
    g1 = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g1["call_log_provider"] == "hotprospector"
    assert g1["last_hp_refresh"] is None and g1["hp_refresh_status"] is None


async def test_never_touches_another_accounts_clients(mock_mongo_client, mock_db):
    await _seed(mock_db)

    out = await update_call_log_provider(_Req({"group_ids": ["other"], "provider": "hotprospector"}), current_user=USER)

    assert out["updated"] == 0
    other = await mock_db["client_groups"].find_one({"id": "other"})
    assert other["call_log_provider"] == "none"


async def test_hotprospector_needs_to_be_connected(mock_mongo_client, mock_db):
    await _seed(mock_db, hp_connected=False)

    with pytest.raises(HTTPException) as e:
        await update_call_log_provider(_Req({"group_ids": ["g1"], "provider": "hotprospector"}), current_user=USER)

    assert e.value.status_code == 400 and "Connect HotProspector" in e.value.detail


async def test_can_switch_back_to_none_without_hotprospector(mock_mongo_client, mock_db):
    await _seed(mock_db, hp_connected=False)

    out = await update_call_log_provider(_Req({"group_ids": ["g1"], "provider": "none"}), current_user=USER)

    assert out["updated"] == 1


@pytest.mark.parametrize("body", [
    {"group_ids": ["g1"], "provider": "dialpad"},
    {"group_ids": [], "provider": "ghl"},
    {"provider": "ghl"},
    {"group_ids": "g1", "provider": "ghl"},
])
async def test_rejects_bad_requests(mock_mongo_client, mock_db, body):
    await _seed(mock_db)
    with pytest.raises(HTTPException) as e:
        await update_call_log_provider(_Req(body), current_user=USER)
    assert e.value.status_code == 400
