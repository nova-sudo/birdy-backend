"""
An admin is never sent through onboarding: the wizard would have them connect
integrations and import clients into an account that should hold none.
"""
from routers.onboarding import onboarding_status


async def test_an_admin_is_always_onboarded(mock_mongo_client, mock_db):
    await mock_db["users"].insert_one({"user_id": "admin@birdy.ai", "role": "admin"})

    out = await onboarding_status(current_user="admin@birdy.ai")

    assert out["completed"] is True
    stored = await mock_db["users"].find_one({"user_id": "admin@birdy.ai"})
    assert "onboarding" not in stored          # nothing written for the admin


async def test_a_new_agency_still_gets_the_wizard(mock_mongo_client, mock_db):
    await mock_db["users"].insert_one({"user_id": "owner@agency.com"})

    out = await onboarding_status(current_user="owner@agency.com")

    assert out["completed"] is False
