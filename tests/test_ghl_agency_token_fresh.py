"""
The GHL agency token died for two days (Nov 2026): a refresh token works once,
and three places refreshed it on their own with no lock, one of them in a
background task that Vercel can freeze between GHL rotating the token and us
saving it. ensure_fresh_agency_token is now the only refresh: it refreshes
when the token is close to expiring, one process at a time, and marks a
refused refresh token so the UI can ask for a reconnect.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

import integrations.gohighlevel as gohl
import jobs.ghl_jobs as ghl_jobs

U = "agency@example.com"


async def _seed(db, expires_in=timedelta(minutes=5), **extra):
    agency = {
        "company_id": "co1", "access_token": "old-access", "refresh_token": "r1",
        "expires_at": datetime.now() + expires_in, **extra,
    }
    await db["users"].insert_one({"user_id": U, "integrations": {"gohighlevel": {"agency": agency}}})


@pytest.fixture
def ghl_refresh(monkeypatch):
    calls = []
    state = {"fail": None}

    async def refresh(refresh_token):
        calls.append(refresh_token)
        await asyncio.sleep(0.05)
        if state["fail"]:
            return False, {"error": "invalid_grant", "status_code": state["fail"]}
        n = len(calls) + 1
        return True, {
            "access_token": f"access-{n}", "refresh_token": f"r{n}", "companyId": "co1",
            "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
        }

    monkeypatch.setattr(gohl.ghl_integration, "refresh_agency_token", refresh)
    return calls, state


async def test_fresh_token_is_not_refreshed(mock_db, mock_mongo_client, ghl_refresh):
    calls, _ = ghl_refresh
    await _seed(mock_db, expires_in=timedelta(hours=20))
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert agency["access_token"] == "old-access" and calls == []


async def test_expiring_token_is_refreshed_and_saved(mock_db, mock_mongo_client, ghl_refresh):
    calls, _ = ghl_refresh
    await _seed(mock_db)
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert calls == ["r1"]
    assert agency["access_token"] == "access-2" and agency["refresh_token"] == "r2"
    user = await mock_db["users"].find_one({"user_id": U})
    assert "agency_refresh_lease" not in user["integrations"]["gohighlevel"]


async def test_concurrent_callers_use_the_refresh_token_once(mock_db, mock_mongo_client, ghl_refresh):
    calls, _ = ghl_refresh
    await _seed(mock_db)
    results = await asyncio.gather(*[gohl.ensure_fresh_agency_token(U, mock_mongo_client) for _ in range(5)])
    assert calls == ["r1"]
    assert {r["access_token"] for r in results} == {"access-2"}


async def test_cron_renews_hours_before_expiry(mock_db, mock_mongo_client, ghl_refresh):
    calls, _ = ghl_refresh
    await _seed(mock_db, expires_in=timedelta(hours=6))
    assert (await gohl.ensure_fresh_agency_token(U, mock_mongo_client))["access_token"] == "old-access"
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client, margin=gohl.AGENCY_JOB_REFRESH_MARGIN)
    assert calls == ["r1"] and agency["access_token"] == "access-2"


async def test_refused_refresh_marks_reconnect_and_stops_request_retries(mock_db, mock_mongo_client, ghl_refresh):
    calls, state = ghl_refresh
    state["fail"] = 400
    await _seed(mock_db, expires_in=timedelta(minutes=-5))
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert agency["needs_reconnect"] is True and "invalid_grant" in agency["refresh_error"]

    await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert calls == ["r1"]  # request paths stop hammering a dead token
    await gohl.ensure_fresh_agency_token(U, mock_mongo_client, retry_dead=True)
    assert calls == ["r1", "r1"]  # the cron keeps trying


async def test_successful_refresh_clears_reconnect(mock_db, mock_mongo_client, ghl_refresh):
    await _seed(mock_db, expires_in=timedelta(minutes=-5), needs_reconnect=True, refresh_error="400: x")
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client, retry_dead=True)
    assert agency["access_token"] == "access-2" and not agency.get("needs_reconnect")


async def test_server_error_does_not_mark_reconnect(mock_db, mock_mongo_client, ghl_refresh):
    _, state = ghl_refresh
    state["fail"] = 503
    await _seed(mock_db)
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert agency["access_token"] == "old-access" and not agency.get("needs_reconnect")


async def test_stale_lease_is_taken_over(mock_db, mock_mongo_client, ghl_refresh):
    calls, _ = ghl_refresh
    await _seed(mock_db)
    await mock_db["users"].update_one({"user_id": U}, {"$set": {
        gohl.AGENCY_REFRESH_LEASE: {"id": "dead", "until": datetime.now() - timedelta(seconds=1)},
    }})
    agency = await gohl.ensure_fresh_agency_token(U, mock_mongo_client)
    assert calls == ["r1"] and agency["access_token"] == "access-2"


async def test_token_job_skips_locations_when_the_token_is_dead(mock_db, mock_mongo_client, ghl_refresh, monkeypatch):
    _, state = ghl_refresh
    state["fail"] = 401
    await _seed(mock_db, expires_in=timedelta(minutes=-5))
    minted = []

    async def generate(*a):
        minted.append(a)
        return True, {"access_token": "x"}

    monkeypatch.setattr(gohl.ghl_integration, "generate_location_token", generate)
    await mock_db["users"].update_one({"user_id": U}, {"$set": {"integrations.gohighlevel.subaccounts": {"l1": {}}}})
    await ghl_jobs.refresh_tokens_for_all_users()
    assert minted == []


def test_ghl_token_errors_do_not_sign_the_user_out():
    assert gohl.ghl_error_status(401) == 409 and gohl.ghl_error_status(403) == 409
    assert gohl.ghl_error_status(404) == 404 and gohl.ghl_error_status(None) == 400
