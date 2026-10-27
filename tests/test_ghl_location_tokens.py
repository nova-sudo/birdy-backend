"""
GHL location tokens lost on a reconnect: the token job only renewed tokens it
already had, and the sync returned quietly without one, so the cron stamped
every client "complete" while no contacts arrived (Oct 2026, every client).

Now the job issues a token for every location a client uses, the sync issues
one when it has none, and a sync that still has none fails loudly.
"""

import pytest

import integrations.gohighlevel as gohl
import jobs.ghl_jobs as ghl_jobs
from services import ghl_service

U = "agency@example.com"


def _users(db):
    return db["users"]


async def _seed_agency(db, subaccounts=None):
    gohl_doc = {"agency": {"company_id": "co1", "access_token": "agency-tok", "refresh_token": "r"}}
    if subaccounts is not None:
        gohl_doc["subaccounts"] = subaccounts
    await _users(db).insert_one({"user_id": U, "integrations": {"gohighlevel": gohl_doc}})


@pytest.fixture
def fake_ghl(monkeypatch):
    issued = []

    async def generate(company_id, location_id, access_token):
        issued.append((company_id, location_id, access_token))
        if location_id == "refused":
            return False, {"error": "forbidden"}
        return True, {"access_token": f"tok-{location_id}", "expires_in": 3600}

    async def details(location_id, token):
        return {"name": f"Loc {location_id}"}

    async def count(location_id, token):
        return 7

    monkeypatch.setattr(gohl.ghl_integration, "generate_location_token", generate)
    for mod in (gohl, ghl_jobs):
        monkeypatch.setattr(mod, "fetch_location_details", details)
        monkeypatch.setattr(mod, "get_contact_count_from_ghl", count)
    return issued


async def test_mint_saves_a_token_from_the_agency_token(mock_db, mock_mongo_client, fake_ghl):
    await _seed_agency(mock_db)
    out = await gohl.mint_location_token(U, "loc1", mock_mongo_client)
    assert out["access_token"] == "tok-loc1" and out["name"] == "Loc loc1"
    assert fake_ghl == [("co1", "loc1", "agency-tok")]
    saved = await gohl.get_subaccount_tokens(U, mock_mongo_client)
    assert saved["loc1"]["access_token"] == "tok-loc1"

    assert await gohl.mint_location_token(U, "refused", mock_mongo_client) is None
    assert await gohl.mint_location_token("nobody@x.com", "loc1", mock_mongo_client) is None


async def test_sync_without_a_token_fails_instead_of_passing(mock_db, mock_mongo_client, fake_ghl):
    await _seed_agency(mock_db)
    with pytest.raises(RuntimeError, match="No GHL token for location refused"):
        await ghl_service.fetch_and_cache_ghl_data_optimized("g1", "refused", U, mock_mongo_client)
    assert fake_ghl == [("co1", "refused", "agency-tok")]


async def test_token_job_issues_tokens_for_every_client_location(mock_db, mock_mongo_client, fake_ghl, monkeypatch):
    await _seed_agency(mock_db, subaccounts={"kept": {"access_token": "old"}})
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": U, "ghl_location_id": "kept"},
        {"id": "g2", "user_id": U, "ghl_location_id": "lost"},
        {"id": "g3", "user_id": U, "ghl_location_id": None},
        {"id": "g4", "user_id": "other@x.com", "ghl_location_id": "theirs"},
    ])

    async def refresh_agency(refresh_token):
        return True, {"companyId": "co1", "access_token": "agency-tok", "refresh_token": "r2"}

    monkeypatch.setattr(ghl_jobs.ghl_integration, "refresh_agency_token", refresh_agency)
    await ghl_jobs.refresh_tokens_for_all_users()

    assert sorted(loc for _, loc, _ in fake_ghl) == ["kept", "lost"]
    saved = await gohl.get_subaccount_tokens(U, mock_mongo_client)
    assert saved["lost"]["access_token"] == "tok-lost" and saved["kept"]["access_token"] == "tok-kept"
