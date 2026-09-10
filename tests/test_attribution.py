"""
tests/test_attribution.py
-------------------------
The ad → click → lead join.

The thing under test is not "does a row get written" but the handful of
judgement calls the attribution makes on the customer's behalf, because every
one of them ends up as a number an agency shows its own client:

  - a lead is credited to the last *paid* touch, not the last touch, so an
    organic revisit can't steal credit from the ad that paid for it
  - a visitor who identifies before their contact has synced is queued, not
    dropped — otherwise almost every lead would go unattributed, since the
    tracker reports an email in milliseconds and the GHL sync runs hourly
  - a contact that already existed before the click is matched but flagged, so
    re-engaged customers don't inflate "leads from this ad"
  - a contact is claimed once: the same email arriving on a second visitor is
    the same person coming back, not a second lead
"""

from datetime import datetime, timedelta

import pytest

from core.database import DB_NAME
from services import attribution_service as attr
from services.attribution_service import (
    MATCHES,
    VISITORS,
    attributed_touch,
    carries_visitor_id,
    clean_touch,
    ensure_site_id,
    install_status,
    leads_by_ad,
    match_confidence,
    record_identity,
    record_touch,
    resolve_site,
    run_match_tick,
    try_match_visitor,
)
from services.tracker_script import render_tracker
from utils.phone_normalize import compute_match_keys

SITE_ID = "sitekeyABCDEF123456"
GROUP_ID = "grp_1"
USER_ID = "owner@example.com"
VISITOR = "v_0123456789abcdef"


@pytest.fixture(autouse=True)
def clear_site_cache():
    """resolve_site memoizes site_id → group; tests must not inherit each other's."""
    attr._site_cache.clear()
    yield
    attr._site_cache.clear()


@pytest.fixture
def site():
    return {
        "site_id": SITE_ID,
        "client_group_id": GROUP_ID,
        "client_group_name": "Body Sculpting Ltd",
        "user_id": USER_ID,
        "location_id": "loc_1",
    }


async def _seed_contact(db, *, email=None, phone=None, contact_id="c_1", date_added=None,
                        custom_fields=None):
    await db["ghl_contacts"].insert_one({
        "user_id": USER_ID,
        "client_group_id": GROUP_ID,
        "location_id": "loc_1",
        "contact_id": contact_id,
        "contact_data": {
            "id": contact_id,
            "email": email,
            "phone": phone,
            "dateAdded": (date_added or datetime.utcnow()).isoformat() + "Z",
            **({"customFields": custom_fields} if custom_fields else {}),
        },
        "match_keys": compute_match_keys(email, phone),
    })


def _touch(**kw):
    return clean_touch({"landing_page": "https://client.com/offer", **kw})


# ---------------------------------------------------------------------------
# Site keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_site_id_is_minted_once_and_resolves(mock_mongo_client, mock_db):
    await mock_db["client_groups"].insert_one(
        {"id": GROUP_ID, "user_id": USER_ID, "name": "Body Sculpting Ltd"}
    )
    group = await mock_db["client_groups"].find_one({"id": GROUP_ID})

    first = await ensure_site_id(group, mock_mongo_client)
    stored = await mock_db["client_groups"].find_one({"id": GROUP_ID})
    second = await ensure_site_id(stored, mock_mongo_client)

    assert first == second, "a second call must not rotate a live site key"

    resolved = await resolve_site(first, mock_mongo_client)
    assert resolved["client_group_id"] == GROUP_ID
    assert resolved["user_id"] == USER_ID


@pytest.mark.asyncio
async def test_unknown_or_malformed_site_id_resolves_to_nothing(mock_mongo_client):
    assert await resolve_site("neverseenbefore123", mock_mongo_client) is None
    assert await resolve_site("short", mock_mongo_client) is None
    assert await resolve_site("has spaces and $ymbols!", mock_mongo_client) is None
    assert await resolve_site(None, mock_mongo_client) is None


# ---------------------------------------------------------------------------
# Touches
# ---------------------------------------------------------------------------

def test_clean_touch_keeps_only_known_identifiers():
    touch = clean_touch({
        "ad_id": "12021988",
        "fbclid": "IwAR123",
        "utm_campaign": "September Lead Gen",
        "landing_page": "https://client.com/offer",
        "password": "hunter2",
        "note": "x" * 5000,
    })
    assert touch == {
        "ad_id": "12021988",
        "fbclid": "IwAR123",
        "utm_campaign": "September Lead Gen",
        "landing_page": "https://client.com/offer",
    }


def test_clean_touch_clips_absurd_values():
    touch = clean_touch({"utm_campaign": "y" * 5000})
    assert len(touch["utm_campaign"]) == attr.MAX_VALUE_LEN


@pytest.mark.asyncio
async def test_last_paid_touch_wins_over_a_later_organic_visit(mock_mongo_client, mock_db, site):
    await record_touch(site, VISITOR, _touch(ad_id="111", campaign_id="c1"), mock_mongo_client)
    await record_touch(site, VISITOR, _touch(ad_id="222", campaign_id="c2"), mock_mongo_client)
    # Straight to the site the next day — no ad involved.
    await record_touch(site, VISITOR, _touch(), mock_mongo_client)

    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["first_touch"]["ad_id"] == "111"
    assert visitor["touch_count"] == 3
    assert attributed_touch(visitor)["ad_id"] == "222", (
        "an organic revisit must not take credit from the ad that paid for it"
    )


@pytest.mark.asyncio
async def test_visitor_with_no_ad_click_falls_back_to_first_touch(mock_mongo_client, mock_db, site):
    await record_touch(site, VISITOR, _touch(), mock_mongo_client)
    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert attributed_touch(visitor)["landing_page"] == "https://client.com/offer"
    assert attributed_touch(visitor).get("ad_id") is None


# ---------------------------------------------------------------------------
# Identity → contact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_identify_matches_an_already_synced_contact(mock_mongo_client, mock_db, site):
    await _seed_contact(mock_db, email="john@gmail.com", phone="+44 7700 900123")
    await record_touch(site, VISITOR, _touch(ad_id="839", adset_id="294", campaign_id="492"),
                       mock_mongo_client)

    result = await record_identity(site, VISITOR, "John@Gmail.com", "07700900123", mock_mongo_client)

    assert result["matched"] is True
    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["ad_id"] == "839"
    assert match["adset_id"] == "294"
    assert match["campaign_id"] == "492"
    assert match["visitor_id"] == VISITOR
    assert match["method"] == "email+phone"
    assert match["confidence"] == 99
    assert match["contact_predates_click"] is False


@pytest.mark.asyncio
async def test_identify_queues_when_the_contact_has_not_synced_yet(mock_mongo_client, mock_db, site):
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)

    result = await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)
    assert result["status"] == "pending"
    assert await mock_db[MATCHES].count_documents({}) == 0

    # The hourly GHL sync catches up.
    await _seed_contact(mock_db, email="john@gmail.com")
    tick = await run_match_tick(mock_mongo_client)

    assert tick["matched"] == 1
    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["ad_id"] == "839"
    assert match["method"] == "email"
    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["match_status"] == "matched"
    assert visitor["ghl_contact_id"] == "c_1"


@pytest.mark.asyncio
async def test_visitor_is_given_up_on_after_the_retry_budget(mock_mongo_client, mock_db, site):
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    await record_identity(site, VISITOR, "ghost@gmail.com", None, mock_mongo_client)

    for _ in range(attr.MAX_MATCH_ATTEMPTS):
        await run_match_tick(mock_mongo_client)

    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["match_attempts"] == attr.MAX_MATCH_ATTEMPTS
    assert visitor["match_status"] == "unmatched"

    # And a drained queue costs nothing on the next tick.
    assert (await run_match_tick(mock_mongo_client))["scanned"] == 0


@pytest.mark.asyncio
async def test_a_contact_is_claimed_by_the_first_visitor_only(mock_mongo_client, mock_db, site):
    await _seed_contact(mock_db, email="john@gmail.com")

    await record_touch(site, VISITOR, _touch(ad_id="111"), mock_mongo_client)
    await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)

    # Same person, new device, clicks a different ad and fills the form again.
    second = "v_ffffffffffffffff"
    await record_touch(site, second, _touch(ad_id="222"), mock_mongo_client)
    await record_identity(site, second, "john@gmail.com", None, mock_mongo_client)

    assert await mock_db[MATCHES].count_documents({}) == 1
    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["ad_id"] == "111", "the ad that actually created the lead keeps the credit"


@pytest.mark.asyncio
async def test_contact_older_than_the_click_is_matched_but_flagged(mock_mongo_client, mock_db, site):
    await _seed_contact(
        mock_db, email="old@gmail.com",
        date_added=datetime.utcnow() - timedelta(days=90),
    )
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    await record_identity(site, VISITOR, "old@gmail.com", None, mock_mongo_client)

    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["contact_predates_click"] is True, (
        "an ad cannot have created a contact that existed three months earlier"
    )


@pytest.mark.asyncio
async def test_a_visitor_id_carried_through_the_form_beats_an_identity_match(
    mock_mongo_client, mock_db, site
):
    await _seed_contact(
        mock_db, email="john@gmail.com",
        custom_fields=[{"id": "cf_1", "value": VISITOR}],
    )
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)

    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["method"] == "visitor_id"
    assert match["confidence"] == 100


@pytest.mark.asyncio
async def test_identify_without_a_usable_email_or_phone_is_ignored(mock_mongo_client, mock_db, site):
    result = await record_identity(site, VISITOR, "not-an-email", "123", mock_mongo_client)
    assert result == {"matched": False, "status": "no_keys"}
    assert await mock_db[VISITORS].count_documents({}) == 0


@pytest.mark.asyncio
async def test_re_identifying_a_matched_visitor_does_not_requeue_them(
    mock_mongo_client, mock_db, site
):
    await _seed_contact(mock_db, email="john@gmail.com")
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)

    result = await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)

    assert result["status"] == "already_matched"
    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["match_status"] == "matched"


def test_match_confidence_scores_by_which_keys_overlapped():
    assert match_confidence(["email:a@b.com", "phone:7700900123"],
                            ["email:a@b.com", "phone:7700900123"]) == ("email+phone", 99)
    assert match_confidence(["email:a@b.com"], ["email:a@b.com", "phone:7700900123"]) == ("email", 95)
    assert match_confidence(["phone:7700900123"], ["phone:7700900123"]) == ("phone", 90)
    assert match_confidence(["email:a@b.com"], ["email:c@d.com"]) == ("none", 0)


def test_carries_visitor_id_reads_both_ghl_field_spellings():
    assert carries_visitor_id({"customFields": [{"value": VISITOR}]}, VISITOR)
    assert carries_visitor_id({"customField": [{"fieldValue": VISITOR}]}, VISITOR)
    assert not carries_visitor_id({"customFields": [{"value": "someone else"}]}, VISITOR)
    assert not carries_visitor_id({}, VISITOR)


@pytest.mark.asyncio
async def test_match_does_not_reach_across_client_groups(mock_mongo_client, mock_db, site):
    await mock_db["ghl_contacts"].insert_one({
        "user_id": USER_ID,
        "client_group_id": "some_other_client",
        "contact_id": "c_other",
        "contact_data": {"email": "john@gmail.com", "dateAdded": datetime.utcnow().isoformat()},
        "match_keys": compute_match_keys("john@gmail.com", None),
    })
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    result = await record_identity(site, VISITOR, "john@gmail.com", None, mock_mongo_client)

    assert result["matched"] is False
    assert await mock_db[MATCHES].count_documents({}) == 0


@pytest.mark.asyncio
async def test_match_tick_survives_a_bad_row(mock_mongo_client, mock_db, site):
    """One unmatchable visitor must not stall the queue behind it."""
    await mock_db[VISITORS].insert_one({
        "_id": "v_brokenbrokenbroken",
        "client_group_id": None,          # never gets a group — unmatchable
        "match_keys": ["email:x@y.com"],
        "match_status": "pending",
        "identified_at": datetime.utcnow() - timedelta(minutes=5),
        "match_attempts": 0,
    })
    await _seed_contact(mock_db, email="john@gmail.com")
    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    await mock_db[VISITORS].update_one(
        {"_id": VISITOR},
        {"$set": {"match_status": "pending", "match_keys": ["email:john@gmail.com"],
                  "identified_at": datetime.utcnow()}},
    )

    tick = await run_match_tick(mock_mongo_client)
    assert tick["matched"] == 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leads_by_ad_counts_by_contact_creation_and_names_the_ad(mock_mongo_client, mock_db):
    now = datetime.utcnow()
    await mock_db[MATCHES].insert_many([
        {"user_id": USER_ID, "client_group_id": GROUP_ID, "ghl_contact_id": "c_1",
         "ad_id": "839", "confidence": 95, "contact_created_at": now - timedelta(days=1),
         "contact_predates_click": False},
        {"user_id": USER_ID, "client_group_id": GROUP_ID, "ghl_contact_id": "c_2",
         "ad_id": "839", "confidence": 100, "contact_created_at": now - timedelta(days=2),
         "contact_predates_click": False},
        {"user_id": USER_ID, "client_group_id": GROUP_ID, "ghl_contact_id": "c_3",
         "ad_id": "111", "confidence": 90, "contact_created_at": now - timedelta(days=1),
         "contact_predates_click": False},
        # Outside the window, and an existing contact — neither should count.
        {"user_id": USER_ID, "client_group_id": GROUP_ID, "ghl_contact_id": "c_4",
         "ad_id": "839", "confidence": 95, "contact_created_at": now - timedelta(days=40),
         "contact_predates_click": False},
        {"user_id": USER_ID, "client_group_id": GROUP_ID, "ghl_contact_id": "c_5",
         "ad_id": "839", "confidence": 95, "contact_created_at": now - timedelta(days=1),
         "contact_predates_click": True},
    ])
    await mock_db["facebook_ad_insights"].insert_one(
        {"user_id": USER_ID, "ad_id": "839", "ad_name": "Lose Belly Fat V2"}
    )

    rows = await leads_by_ad(
        USER_ID, GROUP_ID, now - timedelta(days=7), now, mock_mongo_client
    )

    by_ad = {r["ad_id"]: r for r in rows}
    assert by_ad["839"]["leads"] == 2
    assert by_ad["839"]["ad_name"] == "Lose Belly Fat V2"
    assert by_ad["839"]["deterministic_leads"] == 1
    assert by_ad["111"]["leads"] == 1
    assert by_ad["111"]["ad_name"] is None, "an ad Meta hasn't cached yet still reports its leads"
    assert rows[0]["ad_id"] == "839", "ranked by leads"


@pytest.mark.asyncio
async def test_install_status_reports_what_onboarding_ticks(mock_mongo_client, mock_db, site):
    empty = await install_status(GROUP_ID, mock_mongo_client)
    assert empty["installed"] is False
    assert empty["first_ad_click_seen"] is False

    await record_touch(site, VISITOR, _touch(), mock_mongo_client)
    visited = await install_status(GROUP_ID, mock_mongo_client)
    assert visited["installed"] is True
    assert visited["first_ad_click_seen"] is False, "a plain visit is not an ad click"

    await record_touch(site, VISITOR, _touch(ad_id="839"), mock_mongo_client)
    clicked = await install_status(GROUP_ID, mock_mongo_client)
    assert clicked["first_ad_click_seen"] is True
    assert clicked["last_ad_id"] == "839"


# ---------------------------------------------------------------------------
# The snippet itself
# ---------------------------------------------------------------------------

def test_tracker_is_rendered_with_the_account_baked_in():
    js = render_tracker(SITE_ID, "https://api.birdy.ai/t")
    assert f'var SITE = "{SITE_ID}"' in js
    assert 'var API = "https://api.birdy.ai/t"' in js
    assert "__SITE_ID__" not in js and "__ENDPOINT__" not in js


def test_tracker_posts_as_text_plain_so_no_preflight_is_triggered():
    """
    The endpoints deliberately sit outside the app's CORS allowlist. That only
    works while every request the tracker makes is a CORS *simple* request —
    a JSON content-type here would trigger a preflight the API would reject.
    """
    js = render_tracker(SITE_ID, "https://api.birdy.ai/t")
    assert "text/plain;charset=UTF-8" in js
    assert "application/json" not in js


# ---------------------------------------------------------------------------
# The public edge
# ---------------------------------------------------------------------------
#
# These endpoints run on strangers' browsers on customers' own landing pages,
# so the contract is narrow and absolute: never an error, never a stack trace,
# never a different answer for a real and a fake site id.

from contextlib import asynccontextmanager  # noqa: E402

from routers import tracking  # noqa: E402


class FakeRequest:
    def __init__(self, body: bytes = b"", base_url: str = "https://api.birdy.ai/"):
        self._body = body
        self.base_url = base_url

    async def body(self) -> bytes:
        return self._body


@pytest.fixture
def tracking_db(monkeypatch, mock_mongo_client):
    """Point the tracker endpoints at the in-memory Mongo."""

    @asynccontextmanager
    async def _client():
        yield mock_mongo_client

    monkeypatch.setattr(tracking, "get_mongo_client", _client)
    return mock_mongo_client


async def _register_site(db):
    await db["client_groups"].insert_one({
        "id": GROUP_ID, "user_id": USER_ID, "name": "Body Sculpting Ltd",
        "ghl_location_id": "loc_1", "attribution_site_id": SITE_ID,
    })


def _beacon(**payload) -> FakeRequest:
    import json
    return FakeRequest(json.dumps(payload).encode())


@pytest.mark.asyncio
async def test_collect_records_the_click(tracking_db, mock_db):
    await _register_site(mock_db)

    response = await tracking.collect(_beacon(
        site_id=SITE_ID, visitor_id=VISITOR,
        touch={"ad_id": "839", "utm_source": "facebook",
               "landing_page": "https://client.com/offer"},
    ))

    assert response.status_code == 204
    visitor = await mock_db[VISITORS].find_one({"_id": VISITOR})
    assert visitor["first_touch"]["ad_id"] == "839"
    assert visitor["client_group_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_identify_end_to_end_produces_the_match(tracking_db, mock_db):
    await _register_site(mock_db)
    await _seed_contact(mock_db, email="john@gmail.com")

    await tracking.collect(_beacon(
        site_id=SITE_ID, visitor_id=VISITOR, touch={"ad_id": "839"},
    ))
    response = await tracking.identify(_beacon(
        site_id=SITE_ID, visitor_id=VISITOR, email="john@gmail.com", phone=None,
    ))

    assert response.status_code == 204
    match = await mock_db[MATCHES].find_one({"ghl_contact_id": "c_1"})
    assert match["ad_id"] == "839"


@pytest.mark.asyncio
@pytest.mark.parametrize("request_obj", [
    FakeRequest(b""),
    FakeRequest(b"not json at all"),
    FakeRequest(b'["a list, not an object"]'),
    FakeRequest(b'{"site_id": "x" }' + b" " * 9000),          # oversized
    _beacon(site_id=SITE_ID, visitor_id="../../etc/passwd"),   # bad visitor id
    _beacon(site_id="someone-elses-key", visitor_id=VISITOR),  # unknown site
])
async def test_junk_is_swallowed_silently(tracking_db, mock_db, request_obj):
    await _register_site(mock_db)

    assert (await tracking.collect(request_obj)).status_code == 204
    assert (await tracking.identify(request_obj)).status_code == 204
    assert await mock_db[VISITORS].count_documents({}) == 0


@pytest.mark.asyncio
async def test_identify_never_raises_into_a_customers_page(tracking_db, mock_db, monkeypatch):
    await _register_site(mock_db)

    async def _boom(*a, **kw):
        raise RuntimeError("mongo is having a day")

    monkeypatch.setattr(tracking, "record_identity", _boom)
    response = await tracking.identify(_beacon(
        site_id=SITE_ID, visitor_id=VISITOR, email="john@gmail.com",
    ))
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_script_is_served_without_a_database_lookup(mock_db):
    """
    Deliberately not gated on a known site id: this runs on every page load of
    every customer landing page, and a lookup here would cost a round trip per
    visitor to prevent nothing.
    """
    response = await tracking.tracker_script("neverregistered99.js", FakeRequest())

    assert response.status_code == 200
    assert response.media_type.startswith("application/javascript")
    body = response.body.decode()
    assert 'var SITE = "neverregistered99"' in body
    assert 'var API = "https://api.birdy.ai/t"' in body
