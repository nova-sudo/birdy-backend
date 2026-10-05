"""
tests/test_lead_collection.py
-----------------------------
How a client collects leads, and the setup checklist built from it.

The checklist is the actual product on the setup screen. A red cross with no
sentence beside it is worse than no checklist — the value is that each stage's
failure has exactly one likely cause, so nobody has to guess whether the problem
is the tag, the ads, or the form. So what is pinned here is that the *right*
stage is reported as the blocker, and that a client who was never asked to
install anything is never shown a wall of crosses.

Also pinned: a webhook secret already issued survives a re-save. Regenerating
it would silently break a webhook the customer had already wired into a form
tool, and they would find out from an empty leads table days later. New ones
are no longer issued: the form-tool option now means "sends to GoHighLevel".
"""

from datetime import datetime, timedelta

import pytest

from services import lead_collection as lc
from services.attribution_service import MATCHES, VISITORS
from services.tracked_leads import TRACKED_LEADS

USER = "owner@example.com"
GROUP = "grp_1"


async def _group(db, method=lc.METHOD_LANDING_PAGE, **extra):
    doc = {
        "id": GROUP,
        "user_id": USER,
        "name": "Aura",
        "lead_collection": {**lc.DEFAULT, "method": method, **extra},
    }
    await db["client_groups"].insert_one(doc)
    return doc


# ---------------------------------------------------------------------------
# Reading and writing the config
# ---------------------------------------------------------------------------

def test_read_fills_in_every_key():
    assert lc.read({}) == lc.DEFAULT
    assert lc.read({"lead_collection": {"method": "landing_page"}})["method"] == "landing_page"
    # An unconfigured client is "unknown", never assumed.
    assert lc.read({"lead_collection": {}})["method"] == lc.METHOD_UNKNOWN


def test_unknown_values_are_refused_rather_than_stored():
    assert lc.normalize_method("nonsense") == lc.METHOD_UNKNOWN
    assert lc.normalize_method(None) == lc.METHOD_UNKNOWN
    assert lc.normalize_provider("nonsense") is None
    assert lc.normalize_provider("typeform") == "typeform"


def test_the_retired_external_form_reads_as_a_form_tool_sending_to_ghl():
    assert lc.normalize_method("external_form") == lc.METHOD_FORM_TO_GHL
    stored = {"lead_collection": {"method": "external_form", "form_provider": "roasform"}}
    assert lc.read(stored)["method"] == lc.METHOD_FORM_TO_GHL


@pytest.mark.asyncio
async def test_a_form_tool_sending_to_ghl_needs_no_script_or_secret(mock_mongo_client, mock_db):
    await _group(mock_db, method=lc.METHOD_UNKNOWN)

    config = await lc.save(GROUP, lc.METHOD_FORM_TO_GHL, mock_mongo_client,
                           form_provider="roasform")

    assert config["method"] == lc.METHOD_FORM_TO_GHL
    assert config["form_provider"] == "roasform"
    assert config["webhook_secret"] is None
    assert lc.METHOD_FORM_TO_GHL not in lc.NEEDS_SCRIPT
    assert config["configured_at"] is not None


@pytest.mark.asyncio
async def test_saving_the_retired_name_stores_the_new_one(mock_mongo_client, mock_db):
    await _group(mock_db, method=lc.METHOD_UNKNOWN)

    config = await lc.save(GROUP, lc.METHOD_EXTERNAL_FORM, mock_mongo_client)

    assert config["method"] == lc.METHOD_FORM_TO_GHL


@pytest.mark.asyncio
async def test_a_landing_page_needs_no_secret(mock_mongo_client, mock_db):
    await _group(mock_db, method=lc.METHOD_UNKNOWN)

    config = await lc.save(GROUP, lc.METHOD_LANDING_PAGE, mock_mongo_client)

    assert config["webhook_secret"] is None


@pytest.mark.asyncio
async def test_the_secret_survives_a_re_save(mock_mongo_client, mock_db):
    """
    Rotating it here would break a webhook already wired into the form tool, and
    the customer would find out from an empty leads table days later.
    """
    await _group(mock_db, method=lc.METHOD_EXTERNAL_FORM, webhook_secret="already-wired")

    first = await lc.save(GROUP, lc.METHOD_FORM_TO_GHL, mock_mongo_client)
    second = await lc.save(GROUP, lc.METHOD_FORM_TO_GHL, mock_mongo_client,
                           form_provider="jotform")

    assert first["webhook_secret"] == second["webhook_secret"] == "already-wired"


@pytest.mark.asyncio
async def test_the_ghl_push_is_off_unless_asked_for(mock_mongo_client, mock_db):
    await _group(mock_db, method=lc.METHOD_UNKNOWN)

    config = await lc.save(GROUP, lc.METHOD_LANDING_PAGE, mock_mongo_client)
    assert config["push_to_ghl"] is False

    config = await lc.save(GROUP, lc.METHOD_LANDING_PAGE, mock_mongo_client, push_to_ghl=True)
    assert config["push_to_ghl"] is True


# ---------------------------------------------------------------------------
# The checklist
# ---------------------------------------------------------------------------

def _stage(result, key):
    return next(s for s in result["stages"] if s["key"] == key)


@pytest.mark.asyncio
async def test_a_fresh_client_is_blocked_on_the_script(mock_mongo_client, mock_db):
    group = await _group(mock_db)

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["applicable"] is True
    assert result["complete"] is False
    assert result["next_step"] == "script_installed"
    assert "snippet isn't on the page" in _stage(result, "script_installed")["hint"]


@pytest.mark.asyncio
async def test_a_script_with_no_ad_clicks_blames_the_meta_parameters(mock_mongo_client, mock_db):
    group = await _group(mock_db)
    await mock_db[VISITORS].insert_one({
        "_id": "v_seen", "client_group_id": GROUP, "last_seen_at": datetime.utcnow(),
    })

    result = await lc.diagnose(group, mock_mongo_client)

    assert _stage(result, "script_installed")["done"] is True
    assert result["next_step"] == "ad_clicks_seen"
    assert "tracking parameters" in _stage(result, "ad_clicks_seen")["hint"]


@pytest.mark.asyncio
async def test_ad_clicks_with_no_submissions_points_at_the_form(mock_mongo_client, mock_db):
    group = await _group(mock_db)
    await mock_db[VISITORS].insert_one({
        "_id": "v_clicked", "client_group_id": GROUP,
        "last_seen_at": datetime.utcnow(),
        "last_paid_touch": {"ad_id": "839"},
    })

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["next_step"] == "form_submissions"
    # The likely cause is a form embedded from another site, which our script
    # can't read — and the fix is the GoHighLevel option, not a webhook.
    hint = _stage(result, "form_submissions")["hint"]
    assert "iframe" in hint and "GoHighLevel" in hint


@pytest.mark.asyncio
async def test_unattributed_leads_blame_the_ads_not_the_form(mock_mongo_client, mock_db):
    group = await _group(mock_db)
    await mock_db[VISITORS].insert_one({
        "_id": "v_c", "client_group_id": GROUP, "last_seen_at": datetime.utcnow(),
        "last_paid_touch": {"ad_id": "839"},
    })
    await mock_db[TRACKED_LEADS].insert_one({
        "client_group_id": GROUP, "dedupe_key": "email:a@b.com", "ad_id": None,
    })

    result = await lc.diagnose(group, mock_mongo_client)

    assert _stage(result, "form_submissions")["done"] is True
    assert result["next_step"] == "leads_attributed"


@pytest.mark.asyncio
async def test_a_working_client_is_complete_without_a_crm_match(mock_mongo_client, mock_db):
    """
    Matching to GoHighLevel is optional and must not hold the checklist open.
    A form that doesn't feed the CRM is a supported setup, not a fault.
    """
    group = await _group(mock_db)
    await mock_db[VISITORS].insert_one({
        "_id": "v_d", "client_group_id": GROUP, "last_seen_at": datetime.utcnow(),
        "last_paid_touch": {"ad_id": "839"},
    })
    await mock_db[TRACKED_LEADS].insert_one({
        "client_group_id": GROUP, "dedupe_key": "email:a@b.com", "ad_id": "839",
    })

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["complete"] is True
    assert result["next_step"] is None
    assert _stage(result, "crm_matched")["done"] is False
    assert _stage(result, "crm_matched")["optional"] is True


@pytest.mark.asyncio
async def test_a_crm_match_completes_the_last_stage(mock_mongo_client, mock_db):
    group = await _group(mock_db)
    await mock_db[MATCHES].insert_one({"client_group_id": GROUP, "ghl_contact_id": "c_1"})

    result = await lc.diagnose(group, mock_mongo_client)

    assert _stage(result, "crm_matched")["done"] is True


@pytest.mark.asyncio
async def test_an_instant_form_client_is_shown_no_checklist(mock_mongo_client, mock_db):
    """
    They were never asked to install anything. Five red crosses would be telling
    them to fix a setup they don't have.
    """
    group = await _group(mock_db, method=lc.METHOD_INSTANT_FORM)

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["applicable"] is False
    assert result["complete"] is True
    assert result["stages"] == []


@pytest.mark.asyncio
async def test_a_long_dead_install_reads_as_not_installed(mock_mongo_client, mock_db):
    """A site taken down last month should not still report a green tick."""
    group = await _group(mock_db)
    await mock_db[VISITORS].insert_one({
        "_id": "v_old", "client_group_id": GROUP,
        "last_seen_at": datetime.utcnow() - timedelta(days=lc.LOOKBACK_DAYS + 5),
    })

    result = await lc.diagnose(group, mock_mongo_client)

    assert _stage(result, "script_installed")["done"] is False


# ---------------------------------------------------------------------------
# A form tool that sends to GoHighLevel
# ---------------------------------------------------------------------------

def _contact(days_ago=1, ad_id=None):
    added = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    data = {"dateAdded": added}
    if ad_id:
        data["attributionSource"] = {"adId": ad_id, "utmSource": "facebook"}
    return {"client_group_id": GROUP, "contact_data": data}


@pytest.mark.asyncio
async def test_a_form_tool_client_is_never_asked_to_install_anything(mock_mongo_client, mock_db):
    group = await _group(mock_db, method=lc.METHOD_FORM_TO_GHL)

    result = await lc.diagnose(group, mock_mongo_client)

    keys = [s["key"] for s in result["stages"]]
    assert keys == ["ghl_contacts_arriving", "ghl_contacts_attributed"]
    assert "script_installed" not in keys
    assert result["next_step"] == "ghl_contacts_arriving"


@pytest.mark.asyncio
async def test_contacts_without_an_ad_id_point_at_the_form_tool(mock_mongo_client, mock_db):
    group = await _group(mock_db, method=lc.METHOD_FORM_TO_GHL, form_provider="roasform")
    await mock_db["ghl_contacts"].insert_one(_contact())

    result = await lc.diagnose(group, mock_mongo_client)

    assert _stage(result, "ghl_contacts_arriving")["done"] is True
    assert result["next_step"] == "ghl_contacts_attributed"
    hint = _stage(result, "ghl_contacts_attributed")["hint"]
    assert "ROAS Forms" in hint and "ad id" in hint


@pytest.mark.asyncio
async def test_attributed_contacts_complete_the_form_tool_checklist(mock_mongo_client, mock_db):
    group = await _group(mock_db, method=lc.METHOD_FORM_TO_GHL)
    await mock_db["ghl_contacts"].insert_one(_contact(ad_id="120246113041200118"))

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["complete"] is True and result["next_step"] is None


@pytest.mark.asyncio
async def test_old_contacts_do_not_count_as_working_now(mock_mongo_client, mock_db):
    group = await _group(mock_db, method=lc.METHOD_FORM_TO_GHL)
    await mock_db["ghl_contacts"].insert_one(_contact(days_ago=lc.LOOKBACK_DAYS + 5, ad_id="1"))

    result = await lc.diagnose(group, mock_mongo_client)

    assert result["next_step"] == "ghl_contacts_arriving"
