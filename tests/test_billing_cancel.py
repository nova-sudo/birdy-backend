"""
tests/test_billing_cancel.py
-----------------------------
Cancelling and un-cancelling a subscription from inside Birdy.

Both used to be a trip to Whop's hosted portal, on the belief that Whop had no
cancel API. It has one, so these endpoints call it directly. What matters here
is that they cancel the RIGHT things (both memberships, not just the base
plan), at the right TIME (period end, not immediately — the customer has paid
for the period they are in), and that the local mirror reflects the result
without waiting on a webhook that may never arrive.
"""

import contextlib

import pytest
from fastapi import HTTPException

import billing

USER = "owner@agency.com"


@pytest.fixture
def billing_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(billing, "get_mongo_client", fake_client)
    return billing


@pytest.fixture
def whop(monkeypatch):
    """Record what would have been sent to Whop, and let a test make it refuse."""

    class Calls(list):
        pass

    cancels, uncancels = Calls(), Calls()
    cancels.refuse = set()

    async def fake_cancel(membership_id, *, immediate=True):
        cancels.append((membership_id, immediate))
        if membership_id in cancels.refuse:
            raise HTTPException(
                status_code=502,
                detail="Whop rejected the request: Actor is missing all required permissions",
            )
        return {"id": membership_id}

    async def fake_uncancel(membership_id):
        uncancels.append(membership_id)
        return {"id": membership_id}

    monkeypatch.setattr(billing, "cancel_membership", fake_cancel)
    monkeypatch.setattr(billing, "uncancel_membership", fake_uncancel)
    return {"cancels": cancels, "uncancels": uncancels}


async def a_subscriber(db, **subscription):
    sub = {
        "status": "active",
        "plan_id": "growth",
        "whop_membership_id": "mem_base",
        "current_period_end": "2026-10-01T00:00:00+00:00",
        "cancel_at_period_end": False,
        **subscription,
    }
    await db["users"].insert_one({"user_id": USER, "subscription": sub})


# ── cancelling ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelling_ends_the_plan_at_period_end_not_now(billing_api, mock_db, whop):
    """They have paid for the period they are in. Revoking access the moment
    they click would be taking back something they already own."""
    await a_subscriber(mock_db)

    result = await billing.cancel_subscription(current_user=USER)

    assert whop["cancels"] == [("mem_base", False)]  # immediate=False
    assert result["cancel_at_period_end"] is True
    assert result["current_period_end"] == "2026-10-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_the_extra_client_slot_add_on_is_cancelled_too(billing_api, mock_db, whop):
    """Two Whop memberships, one Birdy account. Cancelling only the base leaves
    the add-on billing for slots on a plan that is ending."""
    await a_subscriber(mock_db, whop_extra_membership_id="mem_extra")

    await billing.cancel_subscription(current_user=USER)

    assert [m for m, _ in whop["cancels"]] == ["mem_base", "mem_extra"]


@pytest.mark.asyncio
async def test_the_mirror_is_updated_without_waiting_for_a_webhook(
    billing_api, mock_db, whop
):
    """users.subscription is webhook-fed and nothing reconciles it, so leaving
    the flag to arrive on its own would show the customer a cancellation that
    apparently did not register."""
    await a_subscriber(mock_db)

    await billing.cancel_subscription(current_user=USER)

    stored = (await mock_db["users"].find_one({"user_id": USER}))["subscription"]
    assert stored["cancel_at_period_end"] is True
    # Still inside an active status: cancelling at period end must not revoke
    # access early.
    assert stored["status"] in billing.ACTIVE_STATUSES


@pytest.mark.asyncio
async def test_cancelling_twice_is_not_an_error(billing_api, mock_db, whop):
    """A double-click or a stale second tab, not a failure worth showing."""
    await a_subscriber(mock_db, cancel_at_period_end=True)

    result = await billing.cancel_subscription(current_user=USER)

    assert result["cancel_at_period_end"] is True
    assert whop["cancels"] == []


@pytest.mark.asyncio
async def test_there_is_nothing_to_cancel_without_a_live_subscription(
    billing_api, mock_db, whop
):
    await a_subscriber(mock_db, status="cancelled")

    with pytest.raises(HTTPException) as exc:
        await billing.cancel_subscription(current_user=USER)

    assert exc.value.status_code == 400
    assert whop["cancels"] == []


@pytest.mark.asyncio
async def test_a_subscription_with_no_membership_id_asks_for_support(
    billing_api, mock_db, whop
):
    """Subscribed per the mirror, nothing to cancel against. A dead end the
    customer cannot fix, so it says so rather than reporting a generic error."""
    await a_subscriber(mock_db, whop_membership_id=None)

    with pytest.raises(HTTPException) as exc:
        await billing.cancel_subscription(current_user=USER)

    assert exc.value.status_code == 409
    assert "support" in exc.value.detail


@pytest.mark.asyncio
async def test_the_mirror_is_untouched_if_whop_refuses(billing_api, mock_db, whop):
    """Recording a cancellation Whop did not make would tell someone they had
    stopped paying while they carried on being billed."""
    whop["cancels"].refuse.add("mem_base")
    await a_subscriber(mock_db)

    with pytest.raises(HTTPException) as exc:
        await billing.cancel_subscription(current_user=USER)

    assert exc.value.status_code == 502
    stored = (await mock_db["users"].find_one({"user_id": USER}))["subscription"]
    assert stored["cancel_at_period_end"] is False


# ── un-cancelling ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reactivating_reverses_a_scheduled_cancellation(billing_api, mock_db, whop):
    await a_subscriber(mock_db, cancel_at_period_end=True, whop_extra_membership_id="mem_extra")

    result = await billing.reactivate_subscription(current_user=USER)

    assert whop["uncancels"] == ["mem_base", "mem_extra"]
    assert result["cancel_at_period_end"] is False
    stored = (await mock_db["users"].find_one({"user_id": USER}))["subscription"]
    assert stored["cancel_at_period_end"] is False


@pytest.mark.asyncio
async def test_there_is_nothing_to_reactivate_without_a_pending_cancellation(
    billing_api, mock_db, whop
):
    await a_subscriber(mock_db)

    with pytest.raises(HTTPException) as exc:
        await billing.reactivate_subscription(current_user=USER)

    assert exc.value.status_code == 400
    assert whop["uncancels"] == []
