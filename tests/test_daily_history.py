"""
tests/test_daily_history.py
---------------------------
Daily history for every additive metric (dashkit widgets v3, task 14).

Dashboards chart any metric over day / week / month by summing the daily rows
the client groups carry (meta_daily_spend, ghl_daily_leads, hp_daily_calls). These
pin the rows the three writers now produce and the catalog flags that say which
metrics may be bucketed over time. Nothing here touches a real API or the dev
database: Meta is served by an httpx mock transport, Mongo by mongomock.
"""

import httpx
import pytest

from core.database import DB_NAME
from integrations.facebook_utils.meta_daily_spend import fetch_account_daily_spend
from services.dashboards_catalog import DAILY_SOURCES, build_catalog
from services.ghl_daily_leads import compute_daily_leads
from services.hp_service import _compute_daily_call_series
from services.metric_orchestrator import METRIC_REGISTRY


# --- Meta ----------------------------------------------------------------------

@pytest.fixture
def patch_meta(monkeypatch):
    """Serve a canned Meta response through the module's httpx client."""
    def apply(body, seen=None):
        real = httpx.AsyncClient

        def handler(request):
            if seen is not None:
                seen.append(str(request.url))
            return httpx.Response(200, json=body)

        def factory(*_a, **kw):
            return real(transport=httpx.MockTransport(handler),
                        **{k: v for k, v in kw.items() if k != "timeout"})

        monkeypatch.setattr(
            "integrations.facebook_utils.meta_daily_spend.httpx.AsyncClient", factory
        )
    return apply


async def test_meta_daily_rows_carry_results_and_leads_from_actions(patch_meta):
    seen = []
    patch_meta({"data": [
        {"date_start": "2026-08-15", "spend": "100", "impressions": "5000", "clicks": "90",
         "actions": [
             {"action_type": "link_click", "value": "90"},
             {"action_type": "lead", "value": "7"},
         ]},
        # Same fallback the preset insights use: no plain "lead", a pixel lead instead.
        {"date_start": "2026-08-16", "spend": "50", "impressions": "2500", "clicks": "40",
         "actions": [{"action_type": "offsite_conversion.fb_pixel_lead", "value": "3"}]},
        # Meta omits `actions` on a day with none: a real zero, not a gap.
        {"date_start": "2026-08-17", "spend": "20", "impressions": "900", "clicks": "5"},
    ]}, seen)

    rows = await fetch_account_daily_spend("act_1", "T", "2026-08-15", "2026-08-17")

    assert rows == [
        {"date": "2026-08-15", "spend": 100.0, "impressions": 5000, "clicks": 90,
         "results": 7, "leads": 7},
        {"date": "2026-08-16", "spend": 50.0, "impressions": 2500, "clicks": 40,
         "results": 3, "leads": 3},
        {"date": "2026-08-17", "spend": 20.0, "impressions": 900, "clicks": 5,
         "results": 0, "leads": 0},
    ]
    # reach is a unique count and cannot be summed per day, so it is not requested.
    assert "fields=spend%2Cimpressions%2Cclicks%2Cactions%2Cdate_start" in seen[0]
    assert "reach" not in seen[0]


# --- GHL -----------------------------------------------------------------------

def _contact(added, opps=None, lead_type="contact"):
    data = {"dateAdded": added}
    if opps is not None:
        data["opportunities"] = opps
    return {"user_id": "u1", "location_id": "loc1", "lead_type": lead_type, "contact_data": data}


async def _daily(mock_mongo_client, contacts):
    await mock_mongo_client[DB_NAME]["ghl_contacts"].insert_many(contacts)
    return {r["date"]: r for r in await compute_daily_leads("u1", "loc1", mock_mongo_client)}


async def test_ghl_daily_rows_bucket_won_on_status_change_date_with_fallback(mock_mongo_client):
    rows = await _daily(mock_mongo_client, [
        _contact("2026-07-01T09:00:00.000Z", lead_type="lead", opps=[
            # Won on the 20th, however long ago the contact was added.
            {"status": "won", "monetaryValue": 1500, "lastStatusChangeAt": "2026-07-20T10:00:00Z",
             "updatedAt": "2026-07-25T10:00:00Z"},
            # No lastStatusChangeAt: falls back to updatedAt.
            {"status": "won", "monetaryValue": "500", "updatedAt": "2026-07-22T10:00:00Z"},
            # Neither: falls back to the opportunity's dateAdded.
            {"status": "lost", "dateAdded": "2026-07-23T10:00:00Z"},
            {"status": "abandoned", "lastStatusChangeAt": "2026-07-20T11:00:00Z"},
            {"status": "open", "lastStatusChangeAt": "2026-07-20T12:00:00Z"},
        ]),
        # No date on the opportunity at all: lands on the contact's dateAdded.
        _contact("2026-07-24T08:00:00.000Z", opps=[{"status": "won", "monetaryValue": 100}]),
    ])

    d20, d22, d23, d24 = (rows[d] for d in ("2026-07-20", "2026-07-22", "2026-07-23", "2026-07-24"))
    assert (d20["opps_won"], d20["opps_lost"], d20["opps_abandoned"], d20["opps_open"]) == (1, 0, 1, 1)
    assert d20["opps_total"] == 3
    assert d20["revenue"] == 1500.0
    assert (d22["opps_won"], d22["revenue"]) == (1, 500.0)
    assert d23["opps_lost"] == 1
    assert (d24["opps_won"], d24["revenue"]) == (1, 100.0)
    # revenue counts won opportunities only.
    assert rows["2026-07-23"]["revenue"] == 0.0


async def test_ghl_existing_cohort_keys_are_unchanged(mock_mongo_client):
    """open/won/lost/abandoned keep their lead-day cohort meaning (Leads Hub and
    report blocks read them); the status-change counts live under opps_*."""
    rows = await _daily(mock_mongo_client, [
        _contact("2026-07-01T09:00:00.000Z", lead_type="lead", opps=[
            {"status": "won", "lastStatusChangeAt": "2026-07-20T10:00:00Z"},
        ]),
    ])

    assert rows["2026-07-01"]["won"] == 1 and rows["2026-07-01"]["new_leads"] == 1
    assert rows["2026-07-01"]["opps_won"] == 0
    assert rows["2026-07-20"]["leads"] == 0 and rows["2026-07-20"]["won"] == 0
    assert rows["2026-07-20"]["opps_won"] == 1


# --- HotProspector -------------------------------------------------------------

def _call(iso, status="outbound", duration=60, lead_id="l1", transfer=False):
    return {"call_time_iso": iso, "call_status": status, "duration": duration,
            "lead_id": lead_id, "transfer": transfer}


def test_hp_daily_rows_split_inbound_outbound_answered():
    rows = _compute_daily_call_series([
        _call("2026-07-01T09:00:00", "inbound", 120, "a"),
        _call("2026-07-01T10:00:00", "outbound", 0, "a"),             # unanswered
        _call("2026-07-01T11:00:00", "outbound", 60, "b", transfer=True),
        _call("2026-07-02T09:00:00", "outbound", 30, "a"),
    ])

    d1, d2 = rows
    assert (d1["calls"], d1["inbound"], d1["outbound"]) == (3, 1, 2)
    assert d1["answered"] == 2
    assert d1["transfers"] == 1
    assert d1["talk_time"] == 3.0 and d1["talk_min"] == 3.0
    assert d1["leads_with_calls"] == 2          # a and b, a counted once
    assert (d2["calls"], d2["outbound"], d2["answered"], d2["leads_with_calls"]) == (1, 1, 1, 1)
    # the keys that were already there keep meaning what they meant
    assert d1["called"] == 2 and d2["called"] == 0


# --- Catalog -------------------------------------------------------------------

def _catalog():
    return {m["key"]: m for m in build_catalog()["metrics"]}


def test_catalog_every_metric_with_a_daily_source_has_over_true_and_reach_has_over_false():
    cat = _catalog()
    for key in DAILY_SOURCES:
        assert cat[key]["over"] is True, key
    for key in ("reach", "meta_reach", "frequency"):
        assert cat[key]["over"] is False and cat[key]["additive"] is False, key
    # general GHL metrics that are not windowed, and lifetime HP leads (no created date)
    assert cat["total_tags"]["over"] is False
    assert cat["hp_leads"]["over"] is False


def test_catalog_ratios_have_over_only_when_both_components_do():
    cat = _catalog()
    for key, spec in METRIC_REGISTRY.items():
        if spec.get("aggregation") != "recompute" or key not in cat:
            continue
        num, den = spec["components"]
        expected = cat[num]["over"] and cat[den]["over"]
        assert cat[key]["over"] is expected, key
        assert cat[key]["additive"] is False, key
    for key in ("ctr", "cpc", "cpm", "cpl", "cost_per_result", "cost_per_lead", "ghl_conversion",
                "conversion_rate", "engagement_rate", "hp_answer_rate"):
        assert cat[key]["over"] is True, key
    for key in ("frequency", "hp_connect_rate"):
        assert cat[key]["over"] is False, key


def test_catalog_additive_marks_sums_that_add_up_to_a_whole():
    cat = _catalog()
    for key in ("spend", "clicks", "leads", "ghl_won_opps", "ghl_revenue", "hp_total_calls", "hp_talk_time"):
        assert cat[key]["additive"] is True, key
    assert cat["total_tags"]["additive"] is False


def test_catalog_every_daily_source_names_a_key_a_writer_produces():
    produced = {
        "meta_daily_spend": {"spend", "impressions", "clicks", "results", "leads"},
        "ghl_daily_leads": {"leads", "opps_won", "opps_lost", "opps_open", "opps_abandoned",
                            "opps_total", "revenue"},
        "hp_daily_calls": {"calls", "inbound", "outbound", "answered", "transfers",
                           "talk_time", "leads_with_calls"},
    }
    for metric, src in DAILY_SOURCES.items():
        assert src["key"] in produced[src["field"]], metric


# --- Previous period over rows that predate a key --------------------------------

def test_previous_is_withheld_while_daily_rows_predate_the_metrics_key():
    """Meta rows written before `leads` existed would sum to a fake zero and
    draw a bogus delta on a leads KPI. Until the rows are rewritten, no compare."""
    from datetime import date, timedelta

    from services.dashboards_resolve import _previous_for

    def day(n):
        return (date.today() - timedelta(days=n)).isoformat()

    old = {"id": "g", "meta_daily_spend": [{"date": day(n), "spend": 1.0} for n in range(1, 40)]}
    new = {"id": "g", "meta_daily_spend": [{"date": day(n), "spend": 1.0, "leads": 2} for n in range(1, 40)]}
    metric = {"key": "leads"}
    binding = {"compare": "previous"}

    reader = _previous_for(metric, binding, "last_7d")
    assert reader.covers(old) is False
    assert reader.covers(new) is True
    assert reader.point(new) == 14.0
