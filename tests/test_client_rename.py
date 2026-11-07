"""
tests/test_client_rename.py
---------------------------
Renaming a client from its settings.

The name is not stored in one place: leads, insights, contacts and the activity
feed each keep a copy, and the client page shows and filters on those copies. A
rename that only touched `client_groups` would leave old names on the page, so
these tests cover the copies as well as the validation.
"""

import contextlib

import pytest
from fastapi import HTTPException

import routers.client_groups as client_groups
from core.database import DB_NAME
from core.models import RenameClientRequest

USER = "user@example.com"
OTHER = "someone@else.com"


@pytest.fixture
def db(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(client_groups, "get_mongo_client", fake_client)
    return mock_mongo_client[DB_NAME]


async def a_group(db, group_id="g1", user_id=USER, name="Aura", **extra):
    await db["client_groups"].insert_one({"id": group_id, "user_id": user_id, "name": name, **extra})


async def rename(name, group_id="g1", user=USER):
    return await client_groups.rename_client_group(
        group_id, RenameClientRequest(name=name), current_user=user
    )


# ── validation ────────────────────────────────────────────────────────────


async def test_rename_saves_the_trimmed_name(db):
    await a_group(db)
    result = await rename("  Aura Clinic  ")
    assert result["name"] == "Aura Clinic"
    assert (await db["client_groups"].find_one({"id": "g1"}))["name"] == "Aura Clinic"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 101])
async def test_rename_refuses_a_blank_or_overlong_name(db, bad):
    await a_group(db)
    with pytest.raises(HTTPException) as err:
        await rename(bad)
    assert err.value.status_code == 400
    assert (await db["client_groups"].find_one({"id": "g1"}))["name"] == "Aura"


async def test_rename_accepts_a_name_at_the_limit(db):
    await a_group(db)
    assert (await rename("x" * 100))["name"] == "x" * 100


async def test_rename_cannot_touch_another_users_client(db):
    await a_group(db, user_id=OTHER)
    with pytest.raises(HTTPException) as err:
        await rename("Mine now")
    assert err.value.status_code == 404
    assert (await db["client_groups"].find_one({"id": "g1"}))["name"] == "Aura"


async def test_a_second_client_can_share_the_name(db):
    # Creating a client doesn't refuse a duplicate name, so renaming doesn't either.
    await a_group(db)
    await a_group(db, group_id="g2", name="Taken")
    assert (await rename("Taken"))["name"] == "Taken"


# ── the stored copies ─────────────────────────────────────────────────────


@pytest.mark.parametrize("collection", [
    "facebook_leads",
    "facebook_ad_insights",
    "facebook_adset_insights",
    "facebook_campaign_insights",
    "ghl_contacts",
    "tracked_leads",
])
async def test_rename_updates_client_group_name_copies(db, collection):
    await a_group(db)
    await db[collection].insert_many([
        {"_id": "mine", "user_id": USER, "client_group_id": "g1", "client_group_name": "Aura"},
        {"_id": "sibling", "user_id": USER, "client_group_id": "g2", "client_group_name": "Aura"},
        {"_id": "stranger", "user_id": OTHER, "client_group_id": "g1", "client_group_name": "Aura"},
    ])
    await rename("Aura Clinic")
    assert (await db[collection].find_one({"_id": "mine"}))["client_group_name"] == "Aura Clinic"
    assert (await db[collection].find_one({"_id": "sibling"}))["client_group_name"] == "Aura"
    assert (await db[collection].find_one({"_id": "stranger"}))["client_group_name"] == "Aura"


@pytest.mark.parametrize("collection", ["ai_suggestions", "agent_cards"])
async def test_rename_updates_client_name_copies(db, collection):
    await a_group(db)
    await db[collection].insert_many([
        {"_id": "mine", "user_id": USER, "client_group_id": "g1", "client_name": "Aura"},
        {"_id": "sibling", "user_id": USER, "client_group_id": "g2", "client_name": "Aura"},
    ])
    await rename("Aura Clinic")
    assert (await db[collection].find_one({"_id": "mine"}))["client_name"] == "Aura Clinic"
    assert (await db[collection].find_one({"_id": "sibling"}))["client_name"] == "Aura"


async def test_rename_updates_activity_including_rows_without_a_client_id(db):
    await a_group(db)
    await db["ai_activity"].insert_many([
        {"_id": "by_id", "user_id": USER, "client_group_id": "g1", "client": "Aura"},
        {"_id": "by_name", "user_id": USER, "client_group_id": None, "client": "Aura"},
        {"_id": "sibling", "user_id": USER, "client_group_id": "g2", "client": "Aura"},
    ])
    await rename("Aura Clinic")
    assert (await db["ai_activity"].find_one({"_id": "by_id"}))["client"] == "Aura Clinic"
    assert (await db["ai_activity"].find_one({"_id": "by_name"}))["client"] == "Aura Clinic"
    assert (await db["ai_activity"].find_one({"_id": "sibling"}))["client"] == "Aura"


async def test_rename_updates_hotprospector_leads_at_the_clients_location(db):
    await a_group(db, ghl_location_id="loc1")
    await db["hotprospector_leads"].insert_many([
        {"_id": "mine", "user_id": USER, "ghl_location_id": "loc1", "lead_data": {"client_name": "Aura"}},
        {"_id": "elsewhere", "user_id": USER, "ghl_location_id": "loc2", "lead_data": {"client_name": "Aura"}},
    ])
    await rename("Aura Clinic")
    assert (await db["hotprospector_leads"].find_one({"_id": "mine"}))["lead_data"]["client_name"] == "Aura Clinic"
    assert (await db["hotprospector_leads"].find_one({"_id": "elsewhere"}))["lead_data"]["client_name"] == "Aura"


async def test_rename_updates_only_this_clients_slot_on_an_alert(db):
    await a_group(db)
    await db["alerts"].insert_one({
        "_id": "a1", "user_id": USER,
        "target_group_ids": ["g2", "g1"], "target_group_names": ["Aura", "Aura"],
    })
    await rename("Aura Clinic")
    assert (await db["alerts"].find_one({"_id": "a1"}))["target_group_names"] == ["Aura", "Aura Clinic"]


async def test_a_failed_copy_update_still_saves_the_name(db, monkeypatch):
    # The name itself is saved first. Meta and GHL syncs stamp the current name
    # again, so a failure here must not report the rename as failed.
    await a_group(db)

    async def boom(*args, **kwargs):
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(client_groups.client_rename_svc, "update_name_copies", boom)
    assert (await rename("Aura Clinic"))["name"] == "Aura Clinic"
    assert (await db["client_groups"].find_one({"id": "g1"}))["name"] == "Aura Clinic"
