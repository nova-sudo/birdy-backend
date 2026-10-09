"""
An admin can give an account a different number of clients than its plan
allows. The override replaces the plan's limit, applies with or without a
subscription, survives Whop rewriting the subscription, and is audited.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from billing_middleware import check_client_limit
from billing import billing_status
from routers.admin_console import ClientLimitOverride, set_client_limit

ADMIN = "admin@birdy.ai"
OWNER = "owner@agency.com"


def _active(plan="starter", extra=0):
    return {"status": "active", "plan_id": plan, "plan_name": plan.title(), "extra_clients_paid": extra,
            "max_clients": {"starter": 3, "growth": 10, "scale": 25}[plan]}


async def _seed(db, *, sub=None, override=None, clients=0):
    user = {"user_id": OWNER}
    if sub:
        user["subscription"] = sub
    if override is not None:
        user["client_limit_override"] = {"limit": override}
    await db["users"].insert_many([user, {"user_id": ADMIN, "role": "admin"}])
    if clients:
        await db["client_groups"].insert_many([{"id": f"g{i}", "user_id": OWNER} for i in range(clients)])


# ── enforcement ──────────────────────────────────────────────────────────────

async def test_an_override_raises_the_plan_limit(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("starter"), override=8, clients=3)   # Starter allows 3
    assert await check_client_limit(OWNER, mock_mongo_client) is True


async def test_an_override_is_enforced_as_the_limit(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("scale"), override=5, clients=5)     # Scale would allow 25

    with pytest.raises(HTTPException) as e:
        await check_client_limit(OWNER, mock_mongo_client)

    assert e.value.status_code == 402
    assert e.value.detail["code"] == "CLIENT_LIMIT_REACHED"
    assert e.value.detail["limit"] == 5 and e.value.detail["limit_override"] is True


async def test_an_override_works_without_a_subscription(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=None, override=4, clients=2)
    assert await check_client_limit(OWNER, mock_mongo_client) is True


async def test_without_an_override_the_plan_still_rules(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("starter"), clients=3)
    with pytest.raises(HTTPException) as e:
        await check_client_limit(OWNER, mock_mongo_client)
    assert e.value.detail["limit"] == 3


async def test_billing_status_reports_the_override(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("growth"), override=40, clients=12)
    out = await billing_status(current_user=OWNER)
    assert out["client_limit"] == 40 and out["client_limit_override"] == 40


async def test_billing_status_reports_the_override_without_a_subscription(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=None, override=6)
    out = await billing_status(current_user=OWNER)
    assert out["subscribed"] is False and out["client_limit"] == 6


# ── the admin endpoint ───────────────────────────────────────────────────────

async def test_admin_sets_an_override_and_it_is_audited(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("starter"), clients=2)

    out = await set_client_limit(OWNER, ClientLimitOverride(limit=15, note="pilot"), admin_email=ADMIN)

    assert out["client_limit"] == 15 and out["plan_client_limit"] == 3 and out["client_count"] == 2
    user = await mock_db["users"].find_one({"user_id": OWNER})
    assert user["client_limit_override"]["limit"] == 15
    assert user["client_limit_override"]["set_by"] == ADMIN
    audit = await mock_db["admin_audit"].find_one({"target": OWNER})
    assert audit["action"] == "client_limit_override" and audit["limit"] == 15 and audit["previous"] is None


async def test_admin_removes_an_override(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("growth"), override=50)

    out = await set_client_limit(OWNER, ClientLimitOverride(limit=None), admin_email=ADMIN)

    assert out["client_limit"] == 10
    user = await mock_db["users"].find_one({"user_id": OWNER})
    assert "client_limit_override" not in user
    audit = await mock_db["admin_audit"].find_one({"target": OWNER})
    assert audit["action"] == "client_limit_override_removed" and audit["previous"] == 50


async def test_the_override_survives_whop_rewriting_the_subscription(mock_mongo_client, mock_db):
    await _seed(mock_db, sub=_active("starter"), clients=3)
    await set_client_limit(OWNER, ClientLimitOverride(limit=9), admin_email=ADMIN)

    # A Whop webhook replaces the subscription document wholesale.
    await mock_db["users"].update_one({"user_id": OWNER}, {"$set": {"subscription": _active("starter")}})

    assert await check_client_limit(OWNER, mock_mongo_client) is True


@pytest.mark.parametrize("limit", [-1, 1001])
async def test_out_of_range_limits_are_refused(mock_mongo_client, mock_db, limit):
    await _seed(mock_db)
    with pytest.raises(HTTPException) as e:
        await set_client_limit(OWNER, ClientLimitOverride(limit=limit), admin_email=ADMIN)
    assert e.value.status_code == 400


async def test_unknown_and_admin_accounts_are_refused(mock_mongo_client, mock_db):
    await _seed(mock_db)
    with pytest.raises(HTTPException) as e:
        await set_client_limit("nobody@x.com", ClientLimitOverride(limit=5), admin_email=ADMIN)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await set_client_limit(ADMIN, ClientLimitOverride(limit=5), admin_email=ADMIN)
    assert e.value.status_code == 400
