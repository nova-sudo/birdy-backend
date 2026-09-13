"""
tests/test_tracked_leads.py
---------------------------
Leads captured off a landing page we don't own.

These are the only leads that exist nowhere else. An instant-form lead is in
Meta's API and a GHL-attributed lead is in the CRM, so if we lose one it can be
re-fetched. A form that posts to a spreadsheet or a Zap gives us exactly one
chance to record the person, which is why the rules pinned here are the ones
about not losing and not duplicating:

  - a submission with no email and no phone is not a lead, and storing it would
    inflate every count downstream
  - the same person submitting twice is one lead; forms get double-submitted and
    webhooks get retried
  - a lead inherits the ad from the visitor's touch history when we have it, and
    from the payload when we don't
  - an identified visitor is no longer deleted by the TTL, which used to destroy
    landing-page leads 180 days after capture
"""

from datetime import datetime, timedelta

import pytest

from services import attribution_service as attr
from services.attribution_service import VISITORS, record_identity, record_touch
from services.tracked_leads import (
    SOURCE_TRACKER,
    SOURCE_WEBHOOK,
    TRACKED_LEADS,
    dedupe_key,
    record_tracked_lead,
)

SITE = {
    "site_id": "sitekeyABCDEF123456",
    "client_group_id": "grp_1",
    "client_group_name": "Aura",
    "user_id": "owner@example.com",
    "location_id": "loc_1",
}
VISITOR = "v_0123456789abcdef"


@pytest.fixture(autouse=True)
def clear_site_cache():
    attr._site_cache.clear()
    yield
    attr._site_cache.clear()


def _submission(**over):
    return {
        "name": "Leah Woods",
        "email": "leah@gmail.com",
        "phone": "+44 7700 900123",
        **over,
    }


# ---------------------------------------------------------------------------
# What counts as a lead
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_submission_becomes_a_lead(mock_mongo_client, mock_db):
    result = await record_tracked_lead(SITE, _submission(), SOURCE_TRACKER, mock_mongo_client)

    assert result == {"stored": True, "reason": "created", "dedupe_key": "email:leah@gmail.com"}
    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["name"] == "Leah Woods"
    assert lead["email"] == "leah@gmail.com"
    # Raw as typed — the normalised form is for matching, and you cannot ring
    # someone back on the last ten digits.
    assert lead["phone"] == "+44 7700 900123"
    assert lead["match_keys"] == ["email:leah@gmail.com", "phone:7700900123"]
    assert lead["source"] == SOURCE_TRACKER
    assert lead["pushed_to_ghl"] is False


@pytest.mark.asyncio
async def test_no_email_and_no_phone_is_not_a_lead(mock_mongo_client, mock_db):
    """A form submission with no way to reach the person is a pageview."""
    result = await record_tracked_lead(
        SITE, {"name": "Anonymous"}, SOURCE_TRACKER, mock_mongo_client
    )

    assert result["stored"] is False
    assert result["reason"] == "no_email_or_phone"
    assert await mock_db[TRACKED_LEADS].count_documents({}) == 0


@pytest.mark.asyncio
async def test_an_unusable_email_falls_back_to_the_phone(mock_mongo_client, mock_db):
    result = await record_tracked_lead(
        SITE, _submission(email="not-an-email"), SOURCE_TRACKER, mock_mongo_client
    )

    assert result["dedupe_key"] == "phone:7700900123"


def test_dedupe_key_prefers_email():
    assert dedupe_key("a@b.com", "07700900123") == "email:a@b.com"
    assert dedupe_key(None, "07700900123") == "phone:7700900123"
    assert dedupe_key("", "") is None
    assert dedupe_key("nonsense", "12") is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_double_submit_is_one_lead(mock_mongo_client, mock_db):
    first = await record_tracked_lead(SITE, _submission(), SOURCE_TRACKER, mock_mongo_client)
    second = await record_tracked_lead(SITE, _submission(), SOURCE_TRACKER, mock_mongo_client)

    assert first["reason"] == "created"
    assert second["reason"] == "updated"
    assert await mock_db[TRACKED_LEADS].count_documents({}) == 1


@pytest.mark.asyncio
async def test_the_webhook_dedupes_against_the_script(mock_mongo_client, mock_db):
    """
    Both paths can see the same submission — our script reads the form and the
    form tool also posts it. That is one lead.
    """
    await record_tracked_lead(SITE, _submission(), SOURCE_TRACKER, mock_mongo_client)
    await record_tracked_lead(
        SITE, _submission(), SOURCE_WEBHOOK, mock_mongo_client, provider="typeform"
    )

    assert await mock_db[TRACKED_LEADS].count_documents({}) == 1
    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["source"] == SOURCE_WEBHOOK, "the later write wins"
    assert lead["provider"] == "typeform"


@pytest.mark.asyncio
async def test_two_clients_can_have_the_same_person(mock_mongo_client, mock_db):
    """Dedupe is per client group — one person can be a lead for two agencies."""
    other = {**SITE, "client_group_id": "grp_2", "client_group_name": "Bodi Genie"}
    await record_tracked_lead(SITE, _submission(), SOURCE_TRACKER, mock_mongo_client)
    await record_tracked_lead(other, _submission(), SOURCE_TRACKER, mock_mongo_client)

    assert await mock_db[TRACKED_LEADS].count_documents({}) == 2


# ---------------------------------------------------------------------------
# Which ad gets the credit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lead_inherits_the_visitors_paid_touch(mock_mongo_client, mock_db):
    await record_touch(
        SITE, VISITOR,
        {"ad_id": "111", "campaign_id": "c1", "landing_page": "https://x.com/a"},
        mock_mongo_client,
    )
    await record_touch(SITE, VISITOR, {"ad_id": "222", "campaign_id": "c2"}, mock_mongo_client)
    # Straight to the site the next day, no ad involved.
    await record_touch(SITE, VISITOR, {"landing_page": "https://x.com/b"}, mock_mongo_client)
    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})

    await record_tracked_lead(
        SITE, _submission(), SOURCE_TRACKER, mock_mongo_client, visitor=visitor
    )

    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["ad_id"] == "222", "an organic revisit must not take the credit"
    assert lead["visitor_id"] == VISITOR


@pytest.mark.asyncio
async def test_a_webhook_with_no_visitor_uses_its_own_identifiers(mock_mongo_client, mock_db):
    """
    A form tool posting server-side may have captured the UTMs itself. With no
    visitor there is nothing else to go on.
    """
    await record_tracked_lead(
        SITE,
        _submission(ad_id="839", utm_source="facebook", utm_campaign="September"),
        SOURCE_WEBHOOK, mock_mongo_client,
    )

    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["ad_id"] == "839"
    assert lead["utm_campaign"] == "September"


@pytest.mark.asyncio
async def test_a_lead_with_no_attribution_is_still_a_lead(mock_mongo_client, mock_db):
    """
    Unattributed is a real answer. Refusing the lead would lose a person the
    client actually has to call.
    """
    await record_tracked_lead(SITE, _submission(), SOURCE_WEBHOOK, mock_mongo_client)

    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["ad_id"] is None
    assert lead["email"] == "leah@gmail.com"


@pytest.mark.asyncio
async def test_absurd_field_lengths_are_clipped(mock_mongo_client, mock_db):
    await record_tracked_lead(
        SITE, _submission(name="y" * 5000), SOURCE_WEBHOOK, mock_mongo_client
    )

    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert len(lead["name"]) == 200


# ---------------------------------------------------------------------------
# The TTL that used to eat these leads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_anonymous_visitor_carries_an_expiry(mock_mongo_client, mock_db):
    await record_touch(SITE, VISITOR, {"ad_id": "839"}, mock_mongo_client)

    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["expires_at"] > datetime.utcnow() + timedelta(days=179)


@pytest.mark.asyncio
async def test_identifying_someone_removes_their_expiry(mock_mongo_client, mock_db):
    """
    The TTL used to sit on `last_seen_at` and expired everyone, so a
    landing-page lead vanished 180 days after capture with nothing left to show
    they had ever existed.
    """
    await record_touch(SITE, VISITOR, {"ad_id": "839"}, mock_mongo_client)
    await record_identity(SITE, VISITOR, "leah@gmail.com", None, mock_mongo_client)

    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert "expires_at" not in visitor


# ---------------------------------------------------------------------------
# The webhook endpoint
# ---------------------------------------------------------------------------
#
# Unlike the browser endpoints this one reports failures. It is server-to-server
# with a person wiring it up, and a silent 204 would leave them staring at a
# form that "works" and an empty leads table.

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from routers import tracking  # noqa: E402

SECRET = "s3cr3t-webhook-token"


class JsonRequest:
    def __init__(self, payload, base_url="https://api.birdy.ai/"):
        self._payload = payload
        self.base_url = base_url

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def body(self):
        import json as _json
        return _json.dumps(self._payload).encode()


@pytest.fixture
def tracking_db(monkeypatch, mock_mongo_client):
    @asynccontextmanager
    async def _client():
        yield mock_mongo_client

    monkeypatch.setattr(tracking, "get_mongo_client", _client)
    return mock_mongo_client


async def _register(db, *, secret=SECRET):
    await db["client_groups"].insert_one({
        "id": SITE["client_group_id"],
        "user_id": SITE["user_id"],
        "name": SITE["client_group_name"],
        "ghl_location_id": SITE["location_id"],
        "attribution_site_id": SITE["site_id"],
        "lead_collection": {"method": "external_form", "webhook_secret": secret},
    })


@pytest.mark.asyncio
async def test_the_webhook_stores_a_lead(tracking_db, mock_db):
    await _register(mock_db)

    result = await tracking.form_webhook(
        SITE["site_id"], JsonRequest(_submission(provider="typeform")), f"Bearer {SECRET}"
    )

    assert result["received"] is True
    assert result["reason"] == "created"
    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["email"] == "leah@gmail.com"
    assert lead["provider"] == "typeform"


@pytest.mark.asyncio
async def test_the_webhook_refuses_a_wrong_secret(tracking_db, mock_db):
    await _register(mock_db)

    with pytest.raises(HTTPException) as raised:
        await tracking.form_webhook(
            SITE["site_id"], JsonRequest(_submission()), "Bearer wrong"
        )

    assert raised.value.status_code == 401
    assert await mock_db[TRACKED_LEADS].count_documents({}) == 0


@pytest.mark.asyncio
async def test_the_webhook_fails_closed_with_no_secret_configured(tracking_db, mock_db):
    """
    An unconfigured webhook accepting anonymous posts would let anyone write
    leads into a customer's reporting.
    """
    await _register(mock_db, secret=None)

    with pytest.raises(HTTPException) as raised:
        await tracking.form_webhook(
            SITE["site_id"], JsonRequest(_submission()), f"Bearer {SECRET}"
        )

    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_the_webhook_needs_an_authorization_header(tracking_db, mock_db):
    await _register(mock_db)

    with pytest.raises(HTTPException) as raised:
        await tracking.form_webhook(SITE["site_id"], JsonRequest(_submission()), None)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_an_unknown_site_id_is_a_404(tracking_db, mock_db):
    await _register(mock_db)

    with pytest.raises(HTTPException) as raised:
        await tracking.form_webhook(
            "neverregistered99", JsonRequest(_submission()), f"Bearer {SECRET}"
        )

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_the_webhook_rejects_a_contactless_submission(tracking_db, mock_db):
    """Reported, not swallowed — whoever mapped the fields got them wrong."""
    await _register(mock_db)

    with pytest.raises(HTTPException) as raised:
        await tracking.form_webhook(
            SITE["site_id"], JsonRequest({"name": "Nobody"}), f"Bearer {SECRET}"
        )

    assert raised.value.status_code == 400
    assert "email or a phone" in raised.value.detail


@pytest.mark.asyncio
async def test_the_webhook_inherits_a_visitors_ad(tracking_db, mock_db):
    """The visitor id travelled through the form, so the click is known exactly."""
    await _register(mock_db)
    await record_touch(SITE, VISITOR, {"ad_id": "839"}, tracking_db)

    await tracking.form_webhook(
        SITE["site_id"],
        JsonRequest(_submission(birdy_visitor_id=VISITOR)),
        f"Bearer {SECRET}",
    )

    lead = await mock_db[TRACKED_LEADS].find_one({})
    assert lead["ad_id"] == "839"
    assert lead["visitor_id"] == VISITOR


# ---------------------------------------------------------------------------
# The install page's public payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_details_are_public_for_a_known_site(tracking_db, mock_db):
    await _register(mock_db)

    body = await tracking.install_details(SITE["site_id"], JsonRequest({}))

    assert body["known"] is True
    assert body["client_name"] == "Aura"
    assert SITE["site_id"] in body["snippet"]
    assert body["hit_received"] is False


@pytest.mark.asyncio
async def test_install_details_never_confirm_an_unknown_site(tracking_db, mock_db):
    """
    Same shape, `known: false`. A 404 here would turn the page into an oracle
    for which site ids are real.
    """
    await _register(mock_db)

    body = await tracking.install_details("neverregistered99", JsonRequest({}))

    assert body["known"] is False
    assert body["client_name"] is None
    assert body["snippet"]
