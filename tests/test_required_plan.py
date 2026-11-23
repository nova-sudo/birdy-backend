"""No free tier: every account needs a plan, the first client included. A sandbox account picks
one without paying. Mocks only."""

import pytest
from fastapi import HTTPException

from billing import SandboxPlanRequest, billing_status, sandbox_plan
from billing_middleware import check_client_limit, needs_plan
from routers.onboarding import onboarding_status

OWNER = "owner@agency.com"
SBX = "tester@sandbox.birdy.test"
ADMIN = "admin@birdy.ai"


def _active(plan="starter"):
    return {"status": "active", "plan_id": plan, "plan_name": plan.title(), "extra_clients_paid": 0,
            "max_clients": {"starter": 3, "growth": 10, "scale": 25}[plan]}


async def _user(db, user_id=OWNER, **fields):
    await db["users"].insert_one({"user_id": user_id, **fields})


def test_needs_plan_only_without_a_subscription_or_an_admin_limit():
    assert needs_plan(None) and needs_plan({}) and needs_plan({"subscription": {"status": "canceled"}})
    assert not needs_plan({"subscription": _active()})
    assert not needs_plan({"subscription": {"status": "trialing"}})
    assert not needs_plan({"client_limit_override": {"limit": 5}})


async def test_the_first_client_is_no_longer_free(mock_mongo_client, mock_db):
    await _user(mock_db)
    with pytest.raises(HTTPException) as e:
        await check_client_limit(OWNER, mock_mongo_client)
    assert e.value.status_code == 402 and e.value.detail["code"] == "NO_ACTIVE_SUBSCRIPTION"


async def test_a_plan_allows_clients_up_to_its_limit(mock_mongo_client, mock_db):
    await _user(mock_db, subscription=_active("starter"))
    assert await check_client_limit(OWNER, mock_mongo_client) is True
    await mock_db["client_groups"].insert_many([{"id": f"g{i}", "user_id": OWNER} for i in range(3)])
    with pytest.raises(HTTPException) as e:
        await check_client_limit(OWNER, mock_mongo_client)
    assert e.value.detail["code"] == "CLIENT_LIMIT_REACHED"


async def test_billing_status_says_who_needs_a_plan_and_who_is_sandbox(mock_mongo_client, mock_db):
    await _user(mock_db)
    out = await billing_status(current_user=OWNER)
    assert out["subscribed"] is False and out["needs_plan"] is True and out["sandbox"] is False
    await mock_db["users"].update_one({"user_id": OWNER}, {"$set": {"subscription": _active("growth")}})
    out = await billing_status(current_user=OWNER)
    assert out["subscribed"] is True and out["needs_plan"] is False


async def test_an_admin_granted_limit_is_not_asked_for_a_plan(mock_mongo_client, mock_db):
    await _user(mock_db, client_limit_override={"limit": 6})
    out = await billing_status(current_user=OWNER)
    assert out["subscribed"] is False and out["needs_plan"] is False


async def test_onboarding_status_keeps_an_account_without_a_plan_in_setup(mock_mongo_client, mock_db):
    await _user(mock_db, onboarding={"completed": True})
    out = await onboarding_status(current_user=OWNER)
    assert out["completed"] is True and out["needs_plan"] is True
    await mock_db["users"].update_one({"user_id": OWNER}, {"$set": {"subscription": _active()}})
    assert (await onboarding_status(current_user=OWNER))["needs_plan"] is False


async def test_an_admin_is_never_asked_for_a_plan(mock_mongo_client, mock_db):
    await _user(mock_db, user_id=ADMIN, role="admin")
    out = await onboarding_status(current_user=ADMIN)
    assert out["completed"] is True and out["needs_plan"] is False


async def test_a_sandbox_account_gets_the_plan_it_picks_without_paying(mock_mongo_client, mock_db):
    await _user(mock_db, user_id=SBX)
    before = await billing_status(current_user=SBX)
    assert before["sandbox"] is True and before["needs_plan"] is True
    out = await sandbox_plan(SandboxPlanRequest(plan_id="Growth"), current_user=SBX)
    assert out["subscribed"] is True and out["needs_plan"] is False
    assert out["plan"] == {"id": "growth", "name": "Growth", "max_clients": 10} and out["client_limit"] == 10
    saved = (await mock_db["users"].find_one({"user_id": SBX}))["subscription"]
    assert saved["sandbox"] is True and saved["whop_membership_id"] is None and saved["status"] == "active"
    assert await check_client_limit(SBX, mock_mongo_client) is True


async def test_a_normal_account_cannot_take_a_plan_for_free(mock_mongo_client, mock_db):
    await _user(mock_db)
    with pytest.raises(HTTPException) as e:
        await sandbox_plan(SandboxPlanRequest(plan_id="scale"), current_user=OWNER)
    assert e.value.status_code == 403 and e.value.detail == {"code": "not_sandbox"}
    assert "subscription" not in await mock_db["users"].find_one({"user_id": OWNER})


async def test_an_unknown_plan_is_refused(mock_mongo_client, mock_db):
    await _user(mock_db, user_id=SBX)
    with pytest.raises(HTTPException) as e:
        await sandbox_plan(SandboxPlanRequest(plan_id="enterprise"), current_user=SBX)
    assert e.value.status_code == 422
