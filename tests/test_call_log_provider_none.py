"""
tests/test_call_log_provider_none.py
------------------------------------
The third sales option: "I don't currently call my leads".

That answer is stored on the client group as `call_log_provider: "none"`, and
it is the single signal the rest of Birdy reads to mean "this client has no
call-centre integration". Everything downstream — the Sales Hub's not-available
state, the greyed call metrics on the Portfolio Dashboard, the funnel's Called
stage — keys off it, so the two properties worth pinning here are:

  * it is a 200, not a 400. The Sales Hub asks /api/call_logs for whichever
    client is selected without first checking what that client uses. A 400
    would surface as an error toast on a screen the reader opened on purpose.

  * a genuinely unknown value still 400s. "none" being tolerated must not turn
    the allow-list into a shrug — a stray string in Mongo is a config bug, and
    reading it as "no calls" would hide it permanently.

The empty payload's *shape* matters as much as its status: the frontend renders
its unavailable state off `meta.provider`, so the provider has to come back.
"""

import contextlib
from datetime import datetime

import pytest
from fastapi import HTTPException

import routers.call_logs as call_logs
import routers.client_groups as client_groups

USER = "user@example.com"
GROUP = "g1"
LOC = "loc-1"


@pytest.fixture
def call_logs_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(call_logs, "get_mongo_client", fake_client)
    return call_logs


async def a_group(db, provider, group_id=GROUP, location_id=LOC):
    await db["client_groups"].insert_one({
        "id": group_id,
        "user_id": USER,
        "name": "Aura",
        "ghl_location_id": location_id,
        "call_log_provider": provider,
    })


async def a_call(db, source="ghl", location_id=LOC):
    """One row in the shape the webhook writer stores."""
    await db["call_logs"].insert_one({
        "user_id": USER,
        "source": source,
        "source_event_id": "ev-1",
        "location_id": location_id,
        "direction": "outbound",
        "status": "completed",
        "duration_seconds": 90,
        "started_at": datetime(2026, 9, 1, 10, 0, 0),
    })


async def list_logs(group_id=GROUP, user=USER, **kwargs):
    return await call_logs.list_call_logs(
        group_id=group_id, skip=0, limit=100, source=None, current_user=user, **kwargs
    )


# ── "none" is an answer, not a fault ────────────────────────────────────────


@pytest.mark.asyncio
async def test_none_provider_returns_an_empty_page_rather_than_400(call_logs_api, mock_db):
    await a_group(mock_db, "none")

    page = await list_logs()

    assert page["data"] == []
    assert page["meta"]["total"] == 0
    assert page["meta"]["returned"] == 0
    # The frontend paints "Not available" off this, so it has to survive.
    assert page["meta"]["provider"] == "none"
    assert page["message"]


@pytest.mark.asyncio
async def test_none_wins_over_the_no_location_message(call_logs_api, mock_db):
    """A client with no call centre usually still has a GHL location — it is
    their CRM. Reporting "no linked GHL location" would send the reader off to
    connect something that is already connected."""
    await a_group(mock_db, "none")

    page = await list_logs()

    assert page["meta"]["location_id"] == LOC
    assert "call centre" in page["message"]


@pytest.mark.asyncio
async def test_none_does_not_serve_rows_that_happen_to_exist(call_logs_api, mock_db):
    """Credentials are account-wide, so a client who says they don't call may
    still have rows sitting in call_logs under their location from an earlier
    configuration. The setting wins over the leftovers."""
    await a_group(mock_db, "none")
    await a_call(mock_db)

    page = await list_logs()

    assert page["data"] == []
    assert page["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_stored_casing_and_whitespace_still_read_as_none(call_logs_api, mock_db):
    await a_group(mock_db, "  NONE ")

    page = await list_logs()

    assert page["meta"]["provider"] == "none"
    assert page["data"] == []


# ── the allow-list still bites ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_provider_still_fails_loudly(call_logs_api, mock_db):
    await a_group(mock_db, "aircall")

    with pytest.raises(HTTPException) as err:
        await list_logs()

    assert err.value.status_code == 400
    assert "aircall" in err.value.detail


# ── the providers that do have rows are untouched ───────────────────────────


@pytest.mark.asyncio
async def test_ghl_provider_still_returns_its_rows(call_logs_api, mock_db):
    await a_group(mock_db, "ghl")
    await a_call(mock_db, source="ghl")

    page = await list_logs()

    assert page["meta"]["provider"] == "ghl"
    assert page["meta"]["total"] == 1
    assert page["data"][0]["direction"] == "outbound"


@pytest.mark.asyncio
async def test_hotprospector_provider_still_returns_its_rows(call_logs_api, mock_db):
    await a_group(mock_db, "hotprospector")
    await a_call(mock_db, source="hotprospector")

    page = await list_logs()

    assert page["meta"]["provider"] == "hotprospector"
    assert page["meta"]["total"] == 1


@pytest.mark.asyncio
async def test_an_explicit_source_override_still_wins(call_logs_api, mock_db):
    """?source= exists for debugging a group whose provider field is wrong on
    legacy rows. A "none" group asked explicitly for ghl gets ghl."""
    await a_group(mock_db, "none")
    await a_call(mock_db, source="ghl")

    page = await call_logs.list_call_logs(
        group_id=GROUP, skip=0, limit=100, source="ghl", current_user=USER
    )

    assert page["meta"]["provider"] == "ghl"
    assert page["meta"]["total"] == 1


# ── the signal has to reach the screens that dim on it ──────────────────────


@pytest.fixture
def groups_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(client_groups, "get_mongo_client", fake_client)
    return client_groups


@pytest.mark.asyncio
async def test_client_groups_serves_the_provider(groups_api, mock_db):
    """GET /api/client-groups is what the Sales Hub and the Portfolio Dashboard
    read, and a projected field is the only way they can know a client has no
    call centre. Left out of the projection, every one of them falls back to
    "ghl" and goes on drawing zeroes — the bug this whole change exists to fix,
    reintroduced by omission rather than by logic."""
    await a_group(mock_db, "none", group_id="quiet")
    await a_group(mock_db, "hotprospector", group_id="dialling")

    out = await client_groups.get_client_groups(
        date_preset="last_7d", include_daily=False, current_user=USER
    )

    by_id = {g["id"]: g for g in out["client_groups"]}
    assert by_id["quiet"]["call_log_provider"] == "none"
    assert by_id["dialling"]["call_log_provider"] == "hotprospector"
