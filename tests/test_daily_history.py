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

from datetime import date, timedelta

from core.constants import ghl_date_bounds
from integrations.facebook_utils.meta_daily_spend import fetch_account_daily_spend
from integrations.gohighlevel import compute_daily_opp_rows, compute_opp_stats
from services.dashboards_catalog import DAILY_SOURCES, build_catalog
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

def _ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _days(rows):
    return {r["date"]: r for r in rows}


def test_ghl_daily_rows_bucket_won_on_status_change_date_with_fallback():
    rows = _days(compute_daily_opp_rows([
        # Won on the 20th, however long ago it was created.
        {"status": "won", "monetaryValue": 1500, "createdAt": "2026-07-01T09:00:00Z",
         "lastStatusChangeAt": "2026-07-20T10:00:00Z", "updatedAt": "2026-07-25T10:00:00Z"},
        # No lastStatusChangeAt: falls back to updatedAt.
        {"status": "won", "monetaryValue": "500", "createdAt": "2026-07-01T09:00:00Z",
         "updatedAt": "2026-07-22T10:00:00Z"},
        # Neither: falls back to createdAt (GHL opps have createdAt, not dateAdded).
        {"status": "lost", "createdAt": "2026-07-23T10:00:00Z"},
        {"status": "abandoned", "lastStatusChangeAt": "2026-07-20T11:00:00Z"},
        # An open opp is counted on its createdAt day, as compute_opp_stats does.
        {"status": "open", "createdAt": "2026-07-20T12:00:00Z", "lastStatusChangeAt": "2026-07-28T12:00:00Z"},
        # No date at all: nothing to bucket on.
        {"status": "won", "monetaryValue": 9999},
    ]))

    d20 = rows["2026-07-20"]
    assert (d20["opps_won"], d20["opps_lost"], d20["opps_abandoned"], d20["opps_open"]) == (1, 0, 1, 1)
    assert d20["opps_total"] == 3 and d20["revenue"] == 1500.0
    assert (rows["2026-07-22"]["opps_won"], rows["2026-07-22"]["revenue"]) == (1, 500.0)
    assert rows["2026-07-23"]["opps_lost"] == 1 and rows["2026-07-23"]["revenue"] == 0.0
    assert "2026-07-28" not in rows
    assert sum(r["opps_total"] for r in rows.values()) == 5


@pytest.mark.parametrize("preset", ["last_7d", "last_30d", "this_month", "maximum"])
def test_ghl_daily_opp_rows_sum_to_the_preset_tile(preset):
    """Summing a window's days must equal compute_opp_stats for that window."""
    opps = []
    for n in range(0, 45):
        stamp = f"{_ago(n)}T{(n * 7) % 24:02d}:15:00Z"
        opps += [
            {"status": "won", "monetaryValue": 100 + n, "lastStatusChangeAt": stamp, "createdAt": stamp},
            {"status": "lost", "lastStatusChangeAt": stamp, "createdAt": stamp},
            {"status": "open", "createdAt": stamp, "lastStatusChangeAt": f"{_ago(0)}T01:00:00Z"},
        ]
        if n % 3 == 0:
            opps.append({"status": "abandoned", "lastStatusChangeAt": stamp})
    opps.append({"status": "won", "monetaryValue": 50, "lastStatusChangeAt": "2020-01-01T00:00:00Z"})
    opps.append({"status": "Won", "monetaryValue": 25, "lastStatusChangeAt": f"{_ago(2)}T23:30:00Z"})
    opps.append({"status": "weird", "lastStatusChangeAt": f"{_ago(2)}T10:00:00Z"})

    start, end = ghl_date_bounds(preset)
    tile = compute_opp_stats(opps, start, end)
    rows = [r for r in compute_daily_opp_rows(opps)
            if (start is None or r["date"] >= start) and (end is None or r["date"] <= end)]

    for status in ("won", "lost", "open", "abandoned"):
        assert sum(r[f"opps_{status}"] for r in rows) == tile[status], (preset, status)
    assert sum(r["opps_total"] for r in rows) == tile["total_opportunities"]
    assert sum(r["revenue"] for r in rows) == pytest.approx(tile["won_revenue"], abs=0.01)


def test_ghl_daily_opp_days_use_the_same_string_slice_as_compute_opp_stats():
    """No timezone conversion: the day is the first ten characters, so a 23:30Z
    stamp and a +01:00 offset stamp land where compute_opp_stats windows them."""
    opps = [
        {"status": "won", "monetaryValue": 10, "lastStatusChangeAt": "2026-07-20T23:30:00Z"},
        {"status": "won", "monetaryValue": 20, "lastStatusChangeAt": "2026-07-21T00:30:00+01:00"},
        {"status": "lost", "lastStatusChangeAt": "2026-07-20T23:59:59.999+01:00"},
    ]

    rows = _days(compute_daily_opp_rows(opps))

    assert rows["2026-07-20"]["opps_won"] == 1 and rows["2026-07-20"]["opps_lost"] == 1
    assert rows["2026-07-21"]["opps_won"] == 1
    for day in ("2026-07-20", "2026-07-21"):
        tile = compute_opp_stats(opps, day, day)
        assert (tile["won"], tile["lost"]) == (rows[day]["opps_won"], rows[day]["opps_lost"])


class _FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 30)


async def test_refresh_stores_daily_opps_next_to_the_preset_stats(mock_mongo_client, mock_db, monkeypatch):
    import services.ghl_service as ghl_service

    opps = [
        {"status": "won", "monetaryValue": 300, "lastStatusChangeAt": "2026-09-10T10:00:00Z"},
        {"status": "open", "createdAt": "2026-09-11T10:00:00Z"},
    ]

    async def fake_fetch(location_id, token):
        return True, opps

    monkeypatch.setattr(ghl_service.ghl_integration, "fetch_all_opportunities", fake_fetch)
    monkeypatch.setattr(ghl_service, "date", _FixedDate)
    await mock_db["client_groups"].insert_one({"id": "g1"})

    await ghl_service.cache_ghl_opp_stats_all_presets("g1", "loc", "tok", mock_mongo_client)

    stored = await mock_db["client_groups"].find_one({"id": "g1"})
    assert stored["ghl_daily_opps"] == [
        {"date": "2026-09-10", "opps_won": 1, "opps_lost": 0, "opps_open": 0,
         "opps_abandoned": 0, "opps_total": 1, "revenue": 300.0},
        {"date": "2026-09-11", "opps_won": 0, "opps_lost": 0, "opps_open": 1,
         "opps_abandoned": 0, "opps_total": 1, "revenue": 0.0},
    ]
    # ghl_daily_leads is the Leads Hub's sparse series and gains no opp-only rows
    assert "ghl_daily_leads" not in stored


# --- HotProspector -------------------------------------------------------------

def _call(iso, status="outbound", duration=60, lead_id="l1", transfer=False):
    return {"call_time_iso": iso, "call_status": status, "duration": duration,
            "lead_id": lead_id, "transfer": transfer}


def test_hp_talk_time_is_unrounded_so_days_sum_to_the_total():
    # 70 s per day rounds to 1.2 min a day (36.0 over 30 days) but is 35.0 min really.
    calls = [_call(f"2026-07-{d:02d}T09:00:00", duration=70) for d in range(1, 31)]
    rows = _compute_daily_call_series(calls)

    assert sum(r["talk_time"] for r in rows) == pytest.approx(30 * 70 / 60)
    assert rows[0]["talk_min"] == 1.2          # the Sales Hub figure stays rounded


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
    # unique / distinct counts cannot be summed per day
    for key in ("reach", "meta_reach", "frequency", "hp_leads_with_calls", "hp_connect_rate"):
        assert cat[key]["over"] is False and cat[key]["additive"] is False, key
    assert "hp_leads_with_calls" not in DAILY_SOURCES
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
    for key in ("frequency", "hp_connect_rate"):  # reach / hp_leads / hp_leads_with_calls lack daily sums
        assert cat[key]["over"] is False, key


def test_catalog_additive_marks_sums_that_add_up_to_a_whole():
    cat = _catalog()
    for key in ("spend", "clicks", "leads", "ghl_won_opps", "ghl_revenue", "hp_total_calls", "hp_talk_time"):
        assert cat[key]["additive"] is True, key
    assert cat["total_tags"]["additive"] is False


async def test_catalog_every_daily_source_names_a_key_a_writer_produces(patch_meta):
    """The produced keys come from what the three writers actually emit."""
    patch_meta({"data": [{"date_start": "2026-08-15", "spend": "1", "impressions": "2", "clicks": "1"}]})
    meta_row = (await fetch_account_daily_spend("act_1", "T", "2026-08-15", "2026-08-15"))[0]
    opp_row = compute_daily_opp_rows([{"status": "won", "lastStatusChangeAt": "2026-08-15T00:00:00Z"}])[0]
    call_row = _compute_daily_call_series([_call("2026-08-15T09:00:00")])[0]
    # ghl_daily_leads keeps its pre-T14 shape; its contact count key is `leads`.
    produced = {
        "meta_daily_spend": set(meta_row),
        "ghl_daily_opps": set(opp_row),
        "ghl_daily_leads": {"leads"},
        "hp_daily_calls": set(call_row),
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
