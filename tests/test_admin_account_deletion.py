"""
tests/test_admin_account_deletion.py
------------------------------------
Account deletion after it stopped being self-service.

Users used to be able to delete themselves from Settings (DELETE /api/account,
guarded by nothing more than "you are signed in"). That route is gone; the same
cascade now lives on DELETE /api/admin/users/{user_id} behind require_admin, so
the refusals — who is turned away, and who is protected from being deleted —
matter here as much as the cascade itself.
"""

import contextlib

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import dependencies
import routers.admin_console as admin_console
from core.config import JWT_SECRET, JWT_ALGORITHM
from dependencies import require_admin

ADMIN = "admin@birdy.ai"
OWNER = "owner@agency.com"


class FakeRequest:
    """Just enough Request for the cookie-reading auth dependencies."""

    def __init__(self, cookies):
        self.cookies = cookies


def signed_in_as(email, **extra_claims):
    token = pyjwt.encode(
        {"sub": email, "type": "access", **extra_claims},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    return FakeRequest({"auth_token": token})


@pytest.fixture
def admin_api(mock_mongo_client, monkeypatch):
    """Point both the router and the auth dependency at the in-memory database."""

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(admin_console, "get_mongo_client", fake_client)
    monkeypatch.setattr(dependencies, "get_mongo_client", fake_client)
    return admin_console


async def an_admin(db, email=ADMIN):
    await db["users"].insert_one({"user_id": email, "name": "Support", "role": "admin"})


async def an_owner(db, email=OWNER, **extra):
    await db["users"].insert_one({"user_id": email, "name": "SOUP Marketing", **extra})


async def delete_account(email=OWNER, admin=ADMIN):
    return await admin_console.delete_user_account(email, admin_email=admin)


# ── the guard ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_normal_user_cannot_reach_the_admin_endpoint(admin_api, mock_db):
    """The whole point of the change: being signed in is no longer enough."""
    await an_owner(mock_db)

    with pytest.raises(HTTPException) as exc:
        await require_admin(signed_in_as(OWNER))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_account_left_is_refused(admin_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await require_admin(signed_in_as("ghost@nowhere.com"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_impersonation_session_cannot_delete_accounts(admin_api, mock_db):
    """An admin impersonating an owner runs as that owner; letting the console
    back in would hand every impersonated session admin powers."""
    await an_admin(mock_db)

    with pytest.raises(HTTPException) as exc:
        await require_admin(signed_in_as(ADMIN, act=ADMIN, imp=True))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_admin_passes_the_guard(admin_api, mock_db):
    await an_admin(mock_db)
    assert await require_admin(signed_in_as(ADMIN)) == ADMIN


# ── the cascade ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_account_and_all_its_data_go(admin_api, mock_db):
    await an_owner(mock_db)
    await mock_db["client_groups"].insert_one({"user_id": OWNER, "name": "Client A"})
    await mock_db["facebook_leads"].insert_many([
        {"user_id": OWNER, "lead_id": "1"},
        {"user_id": OWNER, "lead_id": "2"},
    ])
    await mock_db["call_logs"].insert_one({"user_id": OWNER, "call_id": "c1"})

    out = await delete_account()

    assert out["deleted"] is True
    assert await mock_db["users"].find_one({"user_id": OWNER}) is None
    assert await mock_db["client_groups"].count_documents({}) == 0
    assert await mock_db["facebook_leads"].count_documents({}) == 0
    assert await mock_db["call_logs"].count_documents({}) == 0
    assert out["collections"]["facebook_leads"] == 2


@pytest.mark.asyncio
async def test_other_accounts_are_untouched(admin_api, mock_db):
    await an_owner(mock_db)
    await an_owner(mock_db, email="neighbour@agency.com")
    await mock_db["client_groups"].insert_one(
        {"user_id": "neighbour@agency.com", "name": "Theirs"}
    )

    await delete_account()

    assert await mock_db["users"].find_one({"user_id": "neighbour@agency.com"})
    assert await mock_db["client_groups"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_the_deletion_is_audited(admin_api, mock_db):
    """Nothing survives the cascade, so the audit row is the only record that
    the account ever existed."""
    await an_owner(mock_db)
    await mock_db["client_groups"].insert_one({"user_id": OWNER, "name": "Client A"})

    await delete_account()

    entry = await mock_db["admin_audit"].find_one({"action": "account_delete"})
    assert entry["admin"] == ADMIN
    assert entry["target"] == OWNER
    assert entry["deleted"]["client_groups"] == 1


@pytest.mark.asyncio
async def test_an_unknown_account_is_404(admin_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await delete_account("nobody@nowhere.com")
    assert exc.value.status_code == 404


# ── the refusals ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_admin_cannot_delete_themselves(admin_api, mock_db):
    """Self-deletion is exactly what this ticket removed — it does not come
    back just because the person clicking is an admin."""
    await an_admin(mock_db)

    with pytest.raises(HTTPException) as exc:
        await delete_account(ADMIN)

    assert exc.value.status_code == 400
    assert await mock_db["users"].find_one({"user_id": ADMIN})


@pytest.mark.asyncio
async def test_another_admin_cannot_be_deleted_from_the_console(admin_api, mock_db):
    await an_admin(mock_db)
    await an_owner(mock_db, email="colleague@birdy.ai", role="admin")

    with pytest.raises(HTTPException) as exc:
        await delete_account("colleague@birdy.ai")

    assert exc.value.status_code == 400
    assert await mock_db["users"].find_one({"user_id": "colleague@birdy.ai"})


@pytest.fixture
def cancellations(monkeypatch):
    """Record every membership the endpoint cancels, and let a test make Whop
    refuse. Nothing here reaches the real Whop API."""
    class Calls(list):
        """A list of (membership_id, immediate) that also carries the set of
        ids Whop should refuse, so a test needs one fixture, not two."""

    calls = Calls()

    async def fake_cancel(membership_id, *, immediate=True):
        calls.append((membership_id, immediate))
        if membership_id in refuse:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Whop rejected the request: Actor is missing all required "
                    "permissions: membership:cancel"
                ),
            )
        return {"id": membership_id, "status": "cancelled"}

    refuse = set()
    monkeypatch.setattr(admin_console, "cancel_membership", fake_cancel)
    calls.refuse = refuse
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "canceling"])
async def test_a_live_subscription_is_cancelled_then_the_account_goes(
    admin_api, mock_db, cancellations, status
):
    """Deleting an account cancels what it was paying for. This used to be a
    409 telling the admin to go and do it by hand in Whop."""
    await an_owner(mock_db, subscription={"status": status, "whop_membership_id": "mem_1"})
    await mock_db["client_groups"].insert_one({"user_id": OWNER, "name": "Client A"})

    result = await delete_account()

    # Immediate, not at-period-end: the account it belongs to is gone, so
    # "keep access until the period ends" bills nobody for nothing.
    assert cancellations == [("mem_1", True)]
    assert result["cancelled_memberships"] == ["mem_1"]
    assert await mock_db["users"].find_one({"user_id": OWNER}) is None
    assert await mock_db["client_groups"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_the_extra_client_slot_add_on_is_cancelled_too(
    admin_api, mock_db, cancellations
):
    """The base plan and the extra-slot add-on are two Whop memberships against
    one Birdy account. Cancelling only the base leaves the add-on billing a
    customer who no longer has an account."""
    await an_owner(mock_db, subscription={
        "status": "active",
        "whop_membership_id": "mem_base",
        "whop_extra_membership_id": "mem_extra",
    })

    result = await delete_account()

    assert [m for m, _ in cancellations] == ["mem_base", "mem_extra"]
    assert result["cancelled_memberships"] == ["mem_base", "mem_extra"]


@pytest.mark.asyncio
async def test_nothing_is_deleted_if_whop_refuses_the_cancellation(
    admin_api, mock_db, cancellations
):
    """The ordering is the safety property. A missing `membership:cancel` scope
    must not turn into a deleted account with a live subscription behind it."""
    cancellations.refuse.add("mem_1")
    await an_owner(mock_db, subscription={"status": "active", "whop_membership_id": "mem_1"})
    await mock_db["client_groups"].insert_one({"user_id": OWNER, "name": "Client A"})

    with pytest.raises(HTTPException) as exc:
        await delete_account()

    assert exc.value.status_code == 502
    assert "membership:cancel" in exc.value.detail
    assert "NOT deleted" in exc.value.detail
    assert await mock_db["users"].find_one({"user_id": OWNER})
    assert await mock_db["client_groups"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_a_failure_on_the_add_on_names_what_was_already_cancelled(
    admin_api, mock_db, cancellations
):
    """A half-done cancellation is the one outcome an admin cannot infer from a
    bare error, so the message has to say which membership already went."""
    cancellations.refuse.add("mem_extra")
    await an_owner(mock_db, subscription={
        "status": "active",
        "whop_membership_id": "mem_base",
        "whop_extra_membership_id": "mem_extra",
    })

    with pytest.raises(HTTPException) as exc:
        await delete_account()

    assert "mem_base" in exc.value.detail
    assert await mock_db["users"].find_one({"user_id": OWNER})


@pytest.mark.asyncio
async def test_a_live_subscription_with_no_membership_id_is_still_refused(
    admin_api, mock_db, cancellations
):
    """Subscribed according to our mirror, but nothing to cancel against.
    Deleting would throw away the only record of whose membership it is."""
    await an_owner(mock_db, subscription={"status": "active"})

    with pytest.raises(HTTPException) as exc:
        await delete_account()

    assert exc.value.status_code == 409
    assert cancellations == []
    assert await mock_db["users"].find_one({"user_id": OWNER})


@pytest.mark.asyncio
async def test_a_cancelled_subscription_needs_no_whop_call(
    admin_api, mock_db, cancellations
):
    await an_owner(mock_db, subscription={
        "status": "cancelled", "whop_membership_id": "mem_1",
    })

    result = await delete_account()

    assert cancellations == []
    assert result["cancelled_memberships"] == []
    assert await mock_db["users"].find_one({"user_id": OWNER}) is None


@pytest.mark.asyncio
async def test_the_email_is_normalised_the_way_accounts_are_stored(admin_api, mock_db):
    """Rows come from the console table, but the path segment is still free
    text — an admin pasting a capitalised address must not silently 404."""
    await an_owner(mock_db)
    await delete_account("  Owner@Agency.com  ")
    assert await mock_db["users"].find_one({"user_id": OWNER}) is None
