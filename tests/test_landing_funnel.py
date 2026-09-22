"""
tests/test_landing_funnel.py
----------------------------
The landing-page funnel: views → form starts → opt-ins, and the drop-off.

Every assertion here is about a counting rule, because the counting rules are
the product. A funnel whose stages each count a slightly different thing is not
a slightly wrong funnel — it is four numbers that cannot be compared with each
other, which is worse than none:

  - a visit is a browser-*day*, so a four-page session is one view and the same
    person returning tomorrow is a second
  - a form start is once per visitor-day, so tabbing between fields is one
  - an opt-in is once per visitor *ever*, matching how `tracked_leads` dedupes
    the same person's second submission — otherwise a resubmitted form reports
    an opt-in rate above 100%
  - a form start with no page view above it is dropped rather than counted: a
    stage cannot hold people the stage before it never saw
  - instant-form clients get `applicable: false` rather than four zeroes, which
    would read as a landing page that is failing rather than one that does not
    exist
"""

import contextlib
from datetime import datetime, timedelta

import pytest

import routers.attribution as attribution_router
from core.database import DB_NAME
from services import attribution_service as attr
from services import landing_funnel as lf
from services import lead_collection
from services.attribution_service import (
    record_form_start,
    record_opt_in,
    record_touch,
)
from services.landing_funnel import (
    LANDING_DAILY,
    build_exits,
    build_stages,
    day_of,
    funnel,
    previous_window,
    sum_days,
)
from services.tracked_leads import SOURCE_TRACKER, SOURCE_WEBHOOK, record_tracked_lead

GROUP_ID = "grp_1"
USER_ID = "owner@example.com"
SITE_ID = "sitekeyABCDEF123456"

TODAY = datetime(2026, 9, 22, 10, 0)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def site():
    return {
        "site_id": SITE_ID,
        "client_group_id": GROUP_ID,
        "client_group_name": "Body Sculpting Ltd",
        "user_id": USER_ID,
        "location_id": "loc_1",
    }


@pytest.fixture
def frozen_now(monkeypatch):
    """
    Pin 'now' for both modules that stamp days.

    The counters deduplicate on the day key, so a test that straddles midnight
    would otherwise fail once a day for reasons that have nothing to do with
    the code.
    """
    current = {"value": TODAY}

    class FrozenDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return current["value"]

    monkeypatch.setattr(attr, "datetime", FrozenDatetime)
    monkeypatch.setattr(lf, "datetime", FrozenDatetime)
    return current


async def days(db) -> list[dict]:
    return await db[LANDING_DAILY].find({}, {"_id": 0}).sort("day", 1).to_list(None)


async def counters(db, day: str = None) -> dict:
    row = await db[LANDING_DAILY].find_one({"day": day or day_of(TODAY)})
    return {key: (row or {}).get(key, 0) for key in lf.COUNTERS}


# ── views ────────────────────────────────────────────────────────────────────

async def test_first_landing_counts_a_visitor_and_a_view(mock_mongo_client, mock_db, site, frozen_now):
    await record_touch(site, "v_first", {"landing_page": "https://x.com/"}, mock_mongo_client)

    assert await counters(mock_db) == {
        "pageviews": 1, "visitors": 1, "views": 1,
        "ad_views": 0, "form_starts": 0, "opt_ins": 0,
    }


async def test_second_page_in_the_same_session_is_not_a_second_visit(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_same", {"landing_page": "https://x.com/"}, mock_mongo_client)
    await record_touch(site, "v_same", {"landing_page": "https://x.com/pricing"}, mock_mongo_client)

    counts = await counters(mock_db)
    assert counts["pageviews"] == 2
    assert counts["views"] == 1
    assert counts["visitors"] == 1


async def test_returning_tomorrow_is_a_second_visit_but_not_a_second_visitor(
    mock_mongo_client, mock_db, site, frozen_now
):
    frozen_now["value"] = YESTERDAY
    await record_touch(site, "v_back", {}, mock_mongo_client)

    frozen_now["value"] = TODAY
    await record_touch(site, "v_back", {}, mock_mongo_client)

    yesterday, today = await days(mock_db)
    assert (yesterday["visitors"], yesterday["views"]) == (1, 1)
    assert today["views"] == 1
    assert today.get("visitors", 0) == 0


async def test_only_a_landing_carrying_ad_identifiers_counts_as_an_ad_click(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_paid", {"ad_id": "123"}, mock_mongo_client)
    await record_touch(site, "v_organic", {"referrer": "https://google.com"}, mock_mongo_client)

    counts = await counters(mock_db)
    assert counts["views"] == 2
    assert counts["ad_views"] == 1


async def test_a_second_ad_click_the_same_day_is_still_one_visit(
    mock_mongo_client, mock_db, site, frozen_now
):
    """Not Meta's click count, and deliberately so — see the ad_views hint."""
    await record_touch(site, "v_twice", {"ad_id": "123"}, mock_mongo_client)
    await record_touch(site, "v_twice", {"ad_id": "123"}, mock_mongo_client)

    counts = await counters(mock_db)
    assert counts["ad_views"] == 1
    assert counts["pageviews"] == 2


# ── form starts ──────────────────────────────────────────────────────────────

async def test_form_start_counts_once_per_visitor_day(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_start", {}, mock_mongo_client)

    assert await record_form_start(site, "v_start", mock_mongo_client) is True
    assert await record_form_start(site, "v_start", mock_mongo_client) is False

    assert (await counters(mock_db))["form_starts"] == 1


async def test_form_start_counts_again_on_a_new_day(
    mock_mongo_client, mock_db, site, frozen_now
):
    frozen_now["value"] = YESTERDAY
    await record_touch(site, "v_start", {}, mock_mongo_client)
    await record_form_start(site, "v_start", mock_mongo_client)

    frozen_now["value"] = TODAY
    await record_touch(site, "v_start", {}, mock_mongo_client)
    assert await record_form_start(site, "v_start", mock_mongo_client) is True

    assert (await counters(mock_db, day_of(YESTERDAY)))["form_starts"] == 1
    assert (await counters(mock_db))["form_starts"] == 1


async def test_form_start_without_a_landing_is_dropped(
    mock_mongo_client, mock_db, site, frozen_now
):
    """A stage cannot count people the stage above it never saw."""
    assert await record_form_start(site, "v_ghost", mock_mongo_client) is False
    assert await days(mock_db) == []


# ── opt-ins ──────────────────────────────────────────────────────────────────

async def test_opt_in_counts_once_per_visitor_ever(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_lead", {}, mock_mongo_client)

    assert await record_opt_in(site, "v_lead", mock_mongo_client) is True
    assert await record_opt_in(site, "v_lead", mock_mongo_client) is False

    assert (await counters(mock_db))["opt_ins"] == 1


async def test_resubmitting_the_form_is_not_a_second_opt_in(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_lead", {}, mock_mongo_client)
    visitor = await mock_db[attr.VISITORS].find_one({"_id": "v_lead"})

    payload = {"email": "sam@example.com", "phone": "+44 7700 900123"}
    await record_tracked_lead(site, payload, SOURCE_TRACKER, mock_mongo_client, visitor=visitor)
    await record_tracked_lead(site, payload, SOURCE_TRACKER, mock_mongo_client, visitor=visitor)

    assert (await counters(mock_db))["opt_ins"] == 1


async def test_a_webhook_lead_with_no_visitor_is_not_in_the_funnel(
    mock_mongo_client, mock_db, site, frozen_now
):
    """No view above it either — counting it would break the stages apart."""
    await record_tracked_lead(
        site, {"email": "nobody@example.com"}, SOURCE_WEBHOOK, mock_mongo_client, visitor=None
    )

    assert await days(mock_db) == []


async def test_a_webhook_lead_carrying_a_visitor_id_does_count(
    mock_mongo_client, mock_db, site, frozen_now
):
    await record_touch(site, "v_embed", {"ad_id": "9"}, mock_mongo_client)
    visitor = await mock_db[attr.VISITORS].find_one({"_id": "v_embed"})

    await record_tracked_lead(
        site, {"email": "sam@example.com"}, SOURCE_WEBHOOK, mock_mongo_client, visitor=visitor
    )

    assert (await counters(mock_db))["opt_ins"] == 1


# ── shaping ──────────────────────────────────────────────────────────────────

def test_shares_are_of_the_visits_cohort():
    stages = build_stages(
        {"ad_views": 80, "views": 100, "form_starts": 40, "opt_ins": 10}, None
    )
    by_key = {stage["key"]: stage for stage in stages}

    assert by_key["form_starts"]["share"] == 0.4
    assert by_key["opt_ins"]["share"] == 0.1
    # The cohort has no share of itself, and ad clicks are a slice of it rather
    # than a step below it.
    assert by_key["views"]["share"] is None
    assert by_key["ad_views"]["share"] is None


def test_a_stage_with_no_previous_window_carries_no_delta():
    stages = build_stages({"views": 100, "opt_ins": 10}, {"views": 0, "opt_ins": 0})
    assert all("delta" not in stage for stage in stages)


def test_deltas_compare_against_the_previous_window():
    stages = build_stages({"views": 150, "opt_ins": 9}, {"views": 100, "opt_ins": 10})
    by_key = {stage["key"]: stage for stage in stages}

    assert (by_key["views"]["direction"], by_key["views"]["delta"]) == ("up", 50.0)
    assert (by_key["opt_ins"]["direction"], by_key["opt_ins"]["delta"]) == ("down", 10.0)


def test_exits_split_into_the_two_failures_that_need_opposite_fixes():
    exits = build_exits({"views": 1000, "form_starts": 300, "opt_ins": 100})

    assert exits["total"] == 900
    assert exits["abandoned_form"] == 200   # started it, gave up — a form problem
    assert exits["before_form"] == 700      # never tried — a page problem
    assert exits["before_form"] + exits["abandoned_form"] == exits["total"]
    assert exits["rate"] == 0.9


def test_more_opt_ins_than_detected_starts_does_not_go_negative():
    """
    Autofill submits without ever focusing a field, and an embedded form only
    reports a start when the visitor happens to click it. Both make opt_ins
    exceed form_starts, and the halves must still add up.
    """
    exits = build_exits({"views": 100, "form_starts": 5, "opt_ins": 20})

    assert exits["abandoned_form"] == 0
    assert exits["before_form"] == exits["total"] == 80


def test_an_empty_window_is_zeroes_not_blanks():
    totals = sum_days([])
    assert totals == {key: 0 for key in lf.COUNTERS}
    assert build_exits(totals)["rate"] is None


def test_previous_window_is_the_same_length_ending_the_day_before():
    assert previous_window("2026-09-15", "2026-09-21") == ("2026-09-08", "2026-09-14")
    assert previous_window("2026-09-22", "2026-09-22") == ("2026-09-21", "2026-09-21")


async def test_funnel_reads_only_the_window_asked_for(
    mock_mongo_client, mock_db, site, frozen_now
):
    frozen_now["value"] = TODAY - timedelta(days=40)
    await record_touch(site, "v_old", {}, mock_mongo_client)

    frozen_now["value"] = TODAY
    await record_touch(site, "v_new", {}, mock_mongo_client)

    result = await funnel(GROUP_ID, day_of(TODAY), day_of(TODAY), mock_mongo_client)

    assert result["totals"]["views"] == 1
    assert [day["date"] for day in result["days"]] == [day_of(TODAY)]


async def test_embedded_form_clients_are_told_the_start_count_is_a_floor():
    stages = build_stages({"views": 10, "form_starts": 2}, None, embedded_form=True)
    starts = next(s for s in stages if s["key"] == "form_starts")
    assert starts["hint"] == lf.EMBEDDED_FORM_HINT


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def funnel_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(attribution_router, "get_mongo_client", fake_client)
    return attribution_router


async def a_group(db, method=lead_collection.METHOD_LANDING_PAGE):
    await db["client_groups"].insert_one({
        "id": GROUP_ID,
        "user_id": USER_ID,
        "name": "Body Sculpting Ltd",
        "lead_collection": {"method": method},
    })


async def get_funnel(start="2026-09-22", end="2026-09-22"):
    return await attribution_router.get_landing_funnel(
        GROUP_ID, start=start, end=end, current_user=USER_ID
    )


async def test_instant_form_clients_get_no_funnel_at_all(mock_db, funnel_api):
    await a_group(mock_db, lead_collection.METHOD_INSTANT_FORM)

    body = await get_funnel()

    assert body["applicable"] is False
    assert "stages" not in body
    assert "Instant Forms" in body["reason"]


async def test_landing_page_clients_get_one_even_before_any_traffic(mock_db, funnel_api):
    await a_group(mock_db)

    body = await get_funnel()

    assert body["applicable"] is True
    assert body["totals"]["views"] == 0
    assert [stage["key"] for stage in body["stages"]] == [
        "ad_views", "views", "form_starts", "opt_ins"
    ]


async def test_an_undeclared_client_gets_one_once_their_pages_report(
    mock_mongo_client, mock_db, funnel_api, site, frozen_now
):
    """
    Declaring the collection method is a convenience, not a prerequisite. An
    agency that never opened the portal but whose snippet is live still has a
    real funnel, and hiding it would punish them for a form they didn't fill.
    """
    await a_group(mock_db, lead_collection.METHOD_UNKNOWN)
    assert (await get_funnel())["applicable"] is False

    await record_touch(site, "v_undeclared", {}, mock_mongo_client)
    body = await get_funnel()

    assert body["applicable"] is True
    assert body["totals"]["views"] == 1


async def test_another_agencys_client_is_not_readable(mock_db, funnel_api):
    await a_group(mock_db)

    with pytest.raises(Exception) as excinfo:
        await attribution_router.get_landing_funnel(
            GROUP_ID, start="2026-09-22", end="2026-09-22", current_user="someone@else.com"
        )
    assert excinfo.value.status_code == 404


async def test_a_backwards_window_is_rejected(mock_db, funnel_api):
    await a_group(mock_db)

    with pytest.raises(Exception) as excinfo:
        await get_funnel(start="2026-09-22", end="2026-09-01")
    assert excinfo.value.status_code == 400
