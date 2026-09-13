"""
tests/test_ghl_lead_push.py
---------------------------
Copying a captured landing-page lead into the client's own GoHighLevel.

This writes into somebody else's live CRM, which is the most consequential thing
in the whole feature — a careless version fires an agency's "new lead" automation
at people they have been talking to for a month, or fills their database with
duplicates. So what is pinned here is the three guards, in the order of how much
damage each prevents:

  1. a person already in the CRM is never pushed
  2. a lead already pushed is never pushed again
  3. a failed push never loses the lead

Nothing here touches a real GoHighLevel. The HTTP call is stubbed, so these
assert our decisions rather than their API.
"""

from datetime import datetime

import pytest

from services import ghl_lead_push
from services.ghl_lead_push import push_lead, push_pending_for_group
from services.tracked_leads import TRACKED_LEADS
from utils.phone_normalize import compute_match_keys

USER = "owner@example.com"
GROUP = "grp_1"
LOCATION = "loc_1"
TOKEN = "ghl-access-token"


class FakeResponse:
    def __init__(self, status_code=201, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"contact": {"id": "ghl_contact_1"}}
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient, recording what we would have sent."""

    def __init__(self, response=None, raise_error=None):
        self.response = response or FakeResponse()
        self.raise_error = raise_error
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.posted.append({"url": url, "json": json, "headers": headers})
        if self.raise_error:
            raise self.raise_error
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    """Install a stub httpx client and hand the test the recorder."""
    holder = {}

    def _install(response=None, raise_error=None):
        client = FakeClient(response=response, raise_error=raise_error)
        holder["client"] = client
        monkeypatch.setattr(ghl_lead_push.httpx, "AsyncClient", lambda **kw: client)
        return client

    return _install


async def _lead(db, *, email="leah@gmail.com", phone="07700900123",
                name="Leah Woods", pushed=False):
    doc = {
        "user_id": USER,
        "client_group_id": GROUP,
        "source": "tracker",
        "dedupe_key": f"email:{email}",
        "name": name,
        "email": email,
        "phone": phone,
        "match_keys": compute_match_keys(email, phone),
        "ad_id": "839",
        "submitted_at": datetime(2026, 9, 1, 10, 0),
        "pushed_to_ghl": pushed,
        "ghl_contact_id": None,
    }
    result = await db[TRACKED_LEADS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lead_becomes_a_contact(fake_http, mock_db):
    client = fake_http()
    lead = await _lead(mock_db)

    result = await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert result == {"pushed": True, "reason": "created", "contact_id": "ghl_contact_1"}
    sent = client.posted[0]["json"]
    assert sent["locationId"] == LOCATION
    assert sent["firstName"] == "Leah"
    assert sent["lastName"] == "Woods"
    assert sent["email"] == "leah@gmail.com"
    # Labelled so an agency looking at the contact can see where it came from
    # rather than finding a mystery record.
    assert sent["source"] == ghl_lead_push.SOURCE_LABEL
    assert client.posted[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    stored = await mock_db[TRACKED_LEADS].find_one({"_id": lead["_id"]})
    assert stored["pushed_to_ghl"] is True
    assert stored["ghl_contact_id"] == "ghl_contact_1"


@pytest.mark.asyncio
async def test_a_one_word_name_does_not_invent_a_surname(fake_http, mock_db):
    client = fake_http()
    lead = await _lead(mock_db, name="Cher")

    await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert client.posted[0]["json"]["firstName"] == "Cher"
    assert "lastName" not in client.posted[0]["json"]


@pytest.mark.asyncio
async def test_missing_fields_are_omitted_not_nulled(fake_http, mock_db):
    """GHL rejects explicit nulls on some fields."""
    client = fake_http()
    lead = await _lead(mock_db, phone=None, name=None)

    await push_lead(lead, LOCATION, TOKEN, mock_db)

    sent = client.posted[0]["json"]
    assert "phone" not in sent
    assert "firstName" not in sent
    assert sent["email"] == "leah@gmail.com"


# ---------------------------------------------------------------------------
# Guard 1 — never push someone the CRM already has
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_person_already_in_the_crm_is_not_pushed(fake_http, mock_db):
    """
    GoHighLevel would upsert by email anyway, but that still fires the client's
    "new lead" workflows at someone they have been talking to for a month.
    """
    client = fake_http()
    await mock_db["ghl_contacts"].insert_one({
        "client_group_id": GROUP,
        "contact_id": "existing_contact",
        "match_keys": compute_match_keys("leah@gmail.com", "07700900123"),
    })
    lead = await _lead(mock_db)

    result = await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert result["pushed"] is False
    assert result["reason"] == "already_in_crm"
    assert result["contact_id"] == "existing_contact"
    assert client.posted == [], "no request should have been made at all"

    # Marked done so the sweep stops reconsidering them every tick.
    stored = await mock_db[TRACKED_LEADS].find_one({"_id": lead["_id"]})
    assert stored["pushed_to_ghl"] is True
    assert stored["ghl_contact_id"] == "existing_contact"


@pytest.mark.asyncio
async def test_a_contact_on_another_client_is_not_a_match(fake_http, mock_db):
    """Same person, different agency client — they still need pushing here."""
    client = fake_http()
    await mock_db["ghl_contacts"].insert_one({
        "client_group_id": "someone_else",
        "contact_id": "other_contact",
        "match_keys": compute_match_keys("leah@gmail.com", "07700900123"),
    })
    lead = await _lead(mock_db)

    result = await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert result["pushed"] is True
    assert len(client.posted) == 1


# ---------------------------------------------------------------------------
# Guard 2 — never push twice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_sweep_ignores_leads_already_pushed(fake_http, mock_db):
    client = fake_http()
    await _lead(mock_db, email="done@x.com", pushed=True)
    await _lead(mock_db, email="todo@x.com", pushed=False)
    await mock_db["users"].insert_one({
        "user_id": USER,
        "integrations": {"gohighlevel": {"subaccounts": {LOCATION: {"access_token": TOKEN}}}},
    })
    group = {
        "id": GROUP, "user_id": USER, "ghl_location_id": LOCATION,
        "lead_collection": {"push_to_ghl": True},
    }

    summary = await push_pending_for_group(group, _ClientWrapper(mock_db))

    assert summary["considered"] == 1
    assert summary["pushed"] == 1
    assert len(client.posted) == 1
    assert client.posted[0]["json"]["email"] == "todo@x.com"


# ---------------------------------------------------------------------------
# Guard 3 — a failed push never loses the lead
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_push_leaves_the_lead_intact(fake_http, mock_db):
    fake_http(response=FakeResponse(status_code=422, text="bad request"))
    lead = await _lead(mock_db)

    result = await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert result["pushed"] is False
    assert result["reason"] == "http_422"
    stored = await mock_db[TRACKED_LEADS].find_one({"_id": lead["_id"]})
    assert stored is not None, "the lead must survive a failed push"
    assert stored["pushed_to_ghl"] is False, "and stay eligible for a retry"


@pytest.mark.asyncio
async def test_a_network_error_is_caught_not_raised(fake_http, mock_db):
    import httpx
    fake_http(raise_error=httpx.ConnectError("no route to host"))
    lead = await _lead(mock_db)

    result = await push_lead(lead, LOCATION, TOKEN, mock_db)

    assert result == {"pushed": False, "reason": "network_error", "contact_id": None}
    stored = await mock_db[TRACKED_LEADS].find_one({"_id": lead["_id"]})
    assert stored["pushed_to_ghl"] is False


# ---------------------------------------------------------------------------
# Opting in
# ---------------------------------------------------------------------------

class _ClientWrapper:
    """Minimal mongo-client stand-in: push_pending_for_group indexes by db name."""

    def __init__(self, db):
        self._db = db

    def __getitem__(self, _name):
        return self._db


@pytest.mark.asyncio
async def test_a_client_who_did_not_opt_in_is_never_touched(fake_http, mock_db):
    client = fake_http()
    await _lead(mock_db)
    group = {
        "id": GROUP, "user_id": USER, "ghl_location_id": LOCATION,
        "lead_collection": {"push_to_ghl": False},
    }

    summary = await push_pending_for_group(group, _ClientWrapper(mock_db))

    assert summary == {"skipped": "not_enabled"}
    assert client.posted == []


@pytest.mark.asyncio
async def test_a_missing_token_is_a_no_op_not_an_error(fake_http, mock_db):
    """Not something going wrong — just nothing we can do yet."""
    client = fake_http()
    await _lead(mock_db)
    await mock_db["users"].insert_one({"user_id": USER, "integrations": {}})
    group = {
        "id": GROUP, "user_id": USER, "ghl_location_id": LOCATION,
        "lead_collection": {"push_to_ghl": True},
    }

    summary = await push_pending_for_group(group, _ClientWrapper(mock_db))

    assert summary == {"skipped": "no_access_token"}
    assert client.posted == []
