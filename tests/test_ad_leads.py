"""
tests/test_ad_leads.py
----------------------
The unified ad-attributed lead resolver.

Context for why this exists at all: every per-ad lead surface used to read
`facebook_leads`, the Meta instant-form collection. Clients on their own landing
pages have nothing in it and showed spend against 0 leads — while their leads sat
in `ghl_contacts` carrying `attributionSource.adId`, three times over.

What these tests actually pin is the handful of judgement calls the resolver
makes, because each one becomes a number an agency shows its own client:

  - a GHL contact is a lead only if GHL captured an ad id for it
  - a person who came through both an instant form and a GHL funnel is ONE lead
  - ad set and campaign identity comes from Meta, not from GHL's captured
    strings, which are free text frozen at submission time
  - a lead with no email or phone can't be compared to anything, so it is never
    silently collapsed into someone else
"""

from services.ad_leads import (
    LEAD_SOURCE_GHL_ATTRIBUTION,
    LEAD_SOURCE_PIXEL,
    LEAD_SOURCE_INSTANT_FORM,
    LEAD_SOURCE_MIXED,
    LEAD_SOURCE_NONE,
    count_ad_leads,
    count_ad_leads_by_day,
    describe_lead_source,
    fetch_ad_leads,
    pixel_fallback,
    primary_opportunity,
)
from utils.phone_normalize import compute_match_keys

import pytest

USER = "owner@example.com"
GROUP = "grp_1"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

async def _ghl_contact(
    db, *, contact_id="c_1", email="john@gmail.com", phone="+44 7700 900123",
    ad_id="120246113041200118", date_added="2026-09-01T10:00:00.000Z",
    opportunities=None, group=GROUP, attribution=None, first="John", last="Smith",
):
    source = {
        "utmSource": "facebook",
        "sessionSource": "Paid Social",
        "medium": "facebook",
        "campaign": "SOUP - Body Sculpting - 030925 - CBO",
        "campaignId": "120232720006000118",
        "utmMedium": "Celebrity Body Sculpt - Winning",
        "utmContent": "BS - Evergreen - 260526 - W",
        "adSource": "facebook",
    }
    if ad_id is not None:
        source["adId"] = ad_id
    if attribution is not None:
        source = attribution

    await db["ghl_contacts"].insert_one({
        "user_id": USER,
        "client_group_id": group,
        "client_group_name": "Aura",
        "contact_id": contact_id,
        "match_keys": compute_match_keys(email, phone),
        "lead_type": "lead",
        "contact_data": {
            "id": contact_id,
            "firstName": first, "lastName": last,
            "email": email, "phone": phone,
            "dateAdded": date_added,
            "tags": ["bodysculpt"],
            "opportunities": opportunities or [],
            "attributionSource": source,
        },
    })


async def _instant_form_lead(
    db, *, lead_id="l_1", email="john@gmail.com", phone="+44 7700 900123",
    ad_id="120246113041200118", created="2026-09-01T10:00:00+0000", group=GROUP,
):
    await db["facebook_leads"].insert_one({
        "user_id": USER,
        "client_group_id": group,
        "client_group_name": "Konfidence Clinic",
        "ad_account_id": "act_123",
        "match_keys": compute_match_keys(email, phone),
        "lead_data": {
            "id": lead_id,
            "full_name": "John Smith",
            "email": email,
            "phone_number": phone,
            "created_time": created,
            "ad_id": ad_id,
            "ad_name": "Instant Form Ad",
            "adset_id": "as_1",
            "adset_name": "Women 35-55",
            "campaign_id": "cm_1",
            "campaign_name": "September Lead Gen",
            "platform": "fb",
            "field_data": {"what_treatment": "Fat freezing"},
        },
    })


async def _meta_entities(db, *, ad_id="120246113041200118", ad_name="Lose Belly Fat V2",
                         group=GROUP, flat=False):
    """
    Seed the group's Meta cache, which is where ad identity actually lives.

    `flat` writes the legacy `facebook_cache.ads` lists instead of the split
    `facebook_cache.entities` — production has groups on each, and one real
    group has 53 ads in the flat lists with `entities` empty.
    """
    entities = {
        "ads": [{"id": ad_id, "name": ad_name,
                 "adset_id": "120238943811800656", "campaign_id": "120232720006000118"}],
        "adsets": [{"id": "120238943811800656", "name": "Women 30-50 London"}],
        "campaigns": [{"id": "120232720006000118", "name": "September Lead Gen"}],
    }
    cache = dict(entities) if flat else {"entities": entities}
    await db["client_groups"].insert_one({
        "id": group, "user_id": USER, "name": "Aura", "facebook_cache": cache,
    })


# ---------------------------------------------------------------------------
# GHL-attributed leads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_ghl_contact_with_an_ad_id_is_a_lead(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db)

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 1
    lead = rows[0]
    assert lead["lead_source"] == LEAD_SOURCE_GHL_ATTRIBUTION
    assert lead["full_name"] == "John Smith"
    assert lead["email"] == "john@gmail.com"
    assert lead["phone_number"] == "+44 7700 900123"
    assert lead["ad_id"] == "120246113041200118"
    assert lead["campaign_id"] == "120232720006000118"
    assert lead["created_time"] == "2026-09-01T10:00:00.000Z"
    # It *is* the CRM record, so the CRM columns need no join.
    assert lead["ghl_matched"] is True
    assert lead["ghl_contact_id"] == "c_1"
    assert lead["ghl_tags"] == ["bodysculpt"]


@pytest.mark.asyncio
async def test_a_contact_without_an_ad_id_is_not_a_lead(mock_mongo_client, mock_db):
    """A real contact, but nothing can say which ad produced them."""
    await _ghl_contact(mock_db, contact_id="c_no_ad", ad_id=None)
    await _ghl_contact(mock_db, contact_id="c_empty", ad_id="", email="b@b.com", phone="07700900999")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert rows == []


@pytest.mark.asyncio
async def test_opportunity_status_and_revenue_come_through(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, opportunities=[
        {"status": "lost", "monetaryValue": 100},
        {"status": "won", "monetaryValue": 2500},
    ])

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert rows[0]["ghl_opportunity_status"] == "won"
    assert rows[0]["ghl_opportunity_value"] == 2500.0


def test_primary_opportunity_ranks_won_over_everything():
    assert primary_opportunity([{"status": "open"}, {"status": "won", "monetaryValue": 10}]) == ("won", 10.0)
    assert primary_opportunity([{"status": "lost"}, {"status": "open", "monetaryValue": "5"}]) == ("open", 5.0)
    assert primary_opportunity([{"status": "abandoned"}]) == ("abandoned", 0.0)
    assert primary_opportunity([]) == ("", 0.0)
    assert primary_opportunity([{"status": "won", "monetaryValue": "not a number"}]) == ("won", 0.0)


# ---------------------------------------------------------------------------
# Identity enrichment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ad_identity_is_taken_from_meta_not_from_ghls_strings(mock_mongo_client, mock_db):
    """
    GHL has no ad set id at all, and its ad/campaign names are free text frozen
    at submission time. Meta is the authority.
    """
    await _ghl_contact(mock_db)
    await _meta_entities(mock_db)

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    lead = rows[0]
    assert lead["adset_id"] == "120238943811800656", "GHL supplies no ad set id"
    assert lead["adset_name"] == "Women 30-50 London"
    assert lead["ad_name"] == "Lose Belly Fat V2", "Meta's current name wins over GHL's captured one"
    assert lead["campaign_name"] == "September Lead Gen"


@pytest.mark.asyncio
async def test_ghls_strings_are_the_fallback_when_meta_has_no_row(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db)   # no Meta cache for this group

    lead = (await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client))[0]

    assert lead["ad_name"] == "BS - Evergreen - 260526 - W"      # utmContent
    assert lead["adset_name"] == "Celebrity Body Sculpt - Winning"  # utmMedium
    assert lead["campaign_name"] == "SOUP - Body Sculpting - 030925 - CBO"
    assert lead["adset_id"] == "", "an ad set id must never be invented"


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_same_person_from_both_sources_is_one_lead(mock_mongo_client, mock_db):
    """
    A client running an instant form and a GHL funnel (Konfidence Clinic: 3,020
    contacts, 924 instant-form leads) must not count anyone twice.
    """
    await _instant_form_lead(mock_db)
    await _ghl_contact(mock_db, opportunities=[{"status": "won", "monetaryValue": 3000}])

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 1
    lead = rows[0]
    assert lead["lead_source"] == LEAD_SOURCE_INSTANT_FORM, "the form row is canonical"
    assert lead["field_data"] == {"what_treatment": "Fat freezing"}, "only the form row has answers"
    # ...but the CRM state the GHL row carried is not thrown away.
    assert lead["ghl_matched"] is True
    assert lead["ghl_opportunity_status"] == "won"
    assert lead["ghl_opportunity_value"] == 3000.0


@pytest.mark.asyncio
async def test_different_people_are_not_collapsed(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, contact_id="c_1", email="john@gmail.com", phone="07700900123")
    await _ghl_contact(mock_db, contact_id="c_2", email="jane@gmail.com", phone="07700900456")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_lead_with_no_email_or_phone_is_kept(mock_mongo_client, mock_db):
    """
    No match keys means nothing to compare against. Dropping such a row would
    silently lose a real lead; keeping it risks at worst a duplicate we can see.
    """
    await _ghl_contact(mock_db, contact_id="c_a", email=None, phone=None, first="Anon", last="A")
    await _ghl_contact(mock_db, contact_id="c_b", email=None, phone=None, first="Anon", last="B")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_shared_phone_matches_even_when_the_email_differs(mock_mongo_client, mock_db):
    await _instant_form_lead(mock_db, email="john@work.com", phone="+44 7700 900123")
    await _ghl_contact(mock_db, email="john@home.com", phone="07700900123")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_date_window_covers_the_whole_final_day(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, contact_id="c_in", date_added="2026-09-02T23:30:00.000Z",
                       email="in@x.com", phone="07700900001")
    await _ghl_contact(mock_db, contact_id="c_out", date_added="2026-09-03T00:30:00.000Z",
                       email="out@x.com", phone="07700900002")

    rows = await fetch_ad_leads(USER, [GROUP], "2026-09-01", "2026-09-02", mock_mongo_client)

    assert [r["lead_id"] for r in rows] == ["c_in"]


@pytest.mark.asyncio
async def test_a_malformed_date_is_refused_rather_than_widening_the_window(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db)

    with pytest.raises(ValueError):
        await fetch_ad_leads(USER, [GROUP], "not-a-date", None, mock_mongo_client)


@pytest.mark.asyncio
async def test_groups_are_scoped(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, contact_id="c_mine", group=GROUP)
    await _ghl_contact(mock_db, contact_id="c_theirs", group="someone_else",
                       email="other@x.com", phone="07700900777")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert [r["lead_id"] for r in rows] == ["c_mine"]


@pytest.mark.asyncio
async def test_rows_come_back_newest_first(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, contact_id="c_old", date_added="2026-08-01T10:00:00.000Z",
                       email="old@x.com", phone="07700900011")
    await _ghl_contact(mock_db, contact_id="c_new", date_added="2026-09-10T10:00:00.000Z",
                       email="new@x.com", phone="07700900022")

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert [r["lead_id"] for r in rows] == ["c_new", "c_old"]


# ---------------------------------------------------------------------------
# Instant-form leads still get their CRM join
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_instant_form_lead_still_picks_up_its_ghl_contact(mock_mongo_client, mock_db):
    """
    The contact has no ad attribution of its own, so it is not a lead in its own
    right — but it is still the CRM record for this instant-form lead.
    """
    await _instant_form_lead(mock_db)
    await mock_db["ghl_contacts"].insert_one({
        "user_id": USER,
        "client_group_id": GROUP,
        "contact_id": "c_crm",
        "match_keys": compute_match_keys("john@gmail.com", "+44 7700 900123"),
        "contact_data": {
            "dateAdded": "2026-09-01T11:00:00.000Z",
            "tags": ["booked"],
            "opportunities": [{"status": "won", "monetaryValue": 1800}],
            # no attributionSource
        },
    })

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert len(rows) == 1
    assert rows[0]["lead_source"] == LEAD_SOURCE_INSTANT_FORM
    assert rows[0]["ghl_matched"] is True
    assert rows[0]["ghl_opportunity_status"] == "won"
    assert rows[0]["ghl_tags"] == ["booked"]


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_counts_bucket_by_day_and_dedupe(mock_mongo_client, mock_db):
    await _ghl_contact(mock_db, contact_id="c_1", date_added="2026-09-01T10:00:00.000Z",
                       email="a@x.com", phone="07700900101")
    await _ghl_contact(mock_db, contact_id="c_2", date_added="2026-09-01T18:00:00.000Z",
                       email="b@x.com", phone="07700900102")
    await _ghl_contact(mock_db, contact_id="c_3", date_added="2026-09-02T09:00:00.000Z",
                       email="c@x.com", phone="07700900103")
    # Same person as c_3, arriving through an instant form too.
    await _instant_form_lead(mock_db, lead_id="l_dupe", email="c@x.com",
                             phone="07700900103", created="2026-09-02T09:05:00+0000")

    by_day = await count_ad_leads_by_day(USER, [GROUP], None, None, mock_mongo_client)

    assert by_day == {"2026-09-01": 2, "2026-09-02": 1}
    assert await count_ad_leads(USER, [GROUP], None, None, mock_mongo_client) == 3


@pytest.mark.asyncio
async def test_a_client_with_nothing_counts_zero(mock_mongo_client, mock_db):
    assert await count_ad_leads(USER, [GROUP], None, None, mock_mongo_client) == 0
    assert await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client) == []


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------

def test_describe_lead_source_names_what_the_rows_actually_were():
    assert describe_lead_source([]) == LEAD_SOURCE_NONE
    assert describe_lead_source(
        [{"lead_source": LEAD_SOURCE_INSTANT_FORM}]
    ) == LEAD_SOURCE_INSTANT_FORM
    assert describe_lead_source(
        [{"lead_source": LEAD_SOURCE_GHL_ATTRIBUTION}]
    ) == LEAD_SOURCE_GHL_ATTRIBUTION
    assert describe_lead_source([
        {"lead_source": LEAD_SOURCE_INSTANT_FORM},
        {"lead_source": LEAD_SOURCE_GHL_ATTRIBUTION},
    ]) == LEAD_SOURCE_MIXED


# ---------------------------------------------------------------------------
# Names on instant-form leads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_instant_form_name_falls_back_to_the_forms_own_answers(
    mock_mongo_client, mock_db
):
    """
    Meta only fills `full_name` when the form asked for one. A form asking for a
    first name separately leaves it empty — every one of one real account's 924
    leads is like this — and the name column came back blank.
    """
    await mock_db["facebook_leads"].insert_one({
        "user_id": USER,
        "client_group_id": GROUP,
        "match_keys": compute_match_keys("split@x.com", "07700900301"),
        "lead_data": {
            "id": "l_split",
            "full_name": "",
            "email": "split@x.com",
            "phone_number": "07700900301",
            "created_time": "2026-09-01T10:00:00+0000",
            "ad_id": "a_1",
            "field_data": {
                "first name": "Leah",
                "Last Name": "Woods",
                "phone number": "07700900301",
            },
        },
    })

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert rows[0]["full_name"] == "Leah Woods"


@pytest.mark.asyncio
async def test_a_real_full_name_is_left_alone(mock_mongo_client, mock_db):
    await _instant_form_lead(mock_db)

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert rows[0]["full_name"] == "John Smith"


@pytest.mark.asyncio
async def test_a_lead_with_no_name_anywhere_stays_empty(mock_mongo_client, mock_db):
    await mock_db["facebook_leads"].insert_one({
        "user_id": USER,
        "client_group_id": GROUP,
        "match_keys": compute_match_keys("noname@x.com", None),
        "lead_data": {
            "id": "l_noname", "full_name": "", "email": "noname@x.com",
            "created_time": "2026-09-01T10:00:00+0000", "ad_id": "a_1",
            "field_data": {"what_treatment": "Fat freezing"},
        },
    })

    rows = await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client)

    assert rows[0]["full_name"] == ""


@pytest.mark.asyncio
async def test_identity_also_comes_from_the_legacy_flat_cache(mock_mongo_client, mock_db):
    """
    `facebook_cache` is mid-migration to a split `entities` shape, and both are
    still written. One real production group has 53 ads in the flat lists with
    `entities` empty, so reading only the new shape blanks every ad set id.
    """
    await _ghl_contact(mock_db)
    await _meta_entities(mock_db, flat=True)

    lead = (await fetch_ad_leads(USER, [GROUP], None, None, mock_mongo_client))[0]

    assert lead["adset_id"] == "120238943811800656"
    assert lead["adset_name"] == "Women 30-50 London"
    assert lead["ad_name"] == "Lose Belly Fat V2"


# ---------------------------------------------------------------------------
# Meta's own conversion count as a fallback
# ---------------------------------------------------------------------------

def test_resolved_leads_always_beat_the_pixel_count():
    """
    The two are different objects: a pixel conversion is a number Meta reports
    with no person attached, a lead row is a person. Where rows resolve, they
    win, and the gap between them is the coverage gap — not more leads.
    """
    assert pixel_fallback(257, 251) == (257, None)
    assert pixel_fallback(1, 999) == (1, None)


def test_the_pixel_count_stands_in_when_nothing_resolved():
    """
    Real case this was built for: one client showed £347.56 of spend, 190 pixel
    conversions sitting in the cache, and a leads column reading 0.
    """
    assert pixel_fallback(0, 190) == (190, LEAD_SOURCE_PIXEL)


def test_no_leads_and_no_conversions_stays_zero():
    assert pixel_fallback(0, 0) == (0, None)


def test_the_two_are_never_added_together():
    count, _ = pixel_fallback(0, 190)
    assert count == 190, "a sum here would be neither figure"
    count, _ = pixel_fallback(257, 251)
    assert count == 257
